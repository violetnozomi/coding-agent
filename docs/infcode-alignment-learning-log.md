# NZ-Coder 对齐 InfCode 开发学习日志

> 用途：持续记录 NZ-Coder 在对齐 InfCode 过程中发生的代码变化、设计取舍、验证方式和剩余差距。
> 主要读者：NZ-Coder 项目作者，用于复盘实现思路、准备面试和后续继续开发。
> 维护规则：每完成一项 InfCode 对齐，都要更新“总览”和对应的“详细记录”，不能只改代码不改本文档。

## 1. 如何使用这份文档

本文不是普通的版本更新列表。每项记录都要回答以下问题：

1. InfCode 提供了什么能力？
2. NZ-Coder 原来缺少什么？
3. NZ-Coder 最终修改了哪些模块？
4. 为什么采用当前设计，而不是直接复制 InfCode？
5. 如何验证实现正确？
6. 与 InfCode 相比还剩下什么差距？

阅读代码时，建议按每项记录中的“核心调用链”和“关键文件”顺序进行。测试结果是当时完成该项对齐时的快照，后续测试数量增加属于正常现象。

## 2. 对齐进度总览

| 编号 | 日期 | 对齐能力 | 状态 | NZ-Coder 主要结果 | 完成时验证 |
|---|---|---|---|---|---|
| A280 | 2026-08-25 | 真实SWE续片故障收口：Provider fail-fast、验证/风险语义、调查收敛与Repo热路径 | production_verified（续片因余额中止，不计成绩） | 402/鉴权类错误一次调用即fatal且归类`agent_failed`；stdlib兼容导入故障归环境阻塞；closure reserve拒绝不再污染patch risk；source+test定位后12次调查收敛；语言路由读取后台发布状态，不再物化完整索引或等待数据库锁 | A280只得到4题有效推理+3题余额失败，未跑官方harness且不报告分数；新增行为TDD红绿，Repo/HTTP相关144项通过，完整门禁见详细记录 |
| A278 | 2026-08-25 | Provider Turn Ledger、证据完成直停与显式测试不可变约束 | production_verified | 对每个主模型调用记录 investigation/implementation/verification/repair/convergence 原因与结构化产出；当前代 exact acceptance + ledger + completion review 齐全时在 tool boundary 直接结束；“不修改测试”不再被 pytest 路径误解为新增测试要求，并由工具策略硬阻断 | 5 个全新 DeepSeek 真实任务主模型 3/3/3/5/3 calls，全部外部验收通过且零测试改动；同任务失败样本 7 coding + sidecar/64,201 tokens → 3 coding/22,115；完整门禁见详细记录 |
| A277 | 2026-08-25 | 大窗口模型 Replay-Cost Semantic Compaction | production_verified | 将物理窗口与重复回放成本分离；24K provider-visible history 默认触发原子语义压缩，固定 system/tool schema 不计阈值；单真实用户长任务仍保留近期原子尾部，durable-only metadata 不挤占预算；0 可关闭 | A279 首次触发 25,009→5,090 history tokens；真实 DeepSeek 摘要后 continuation=`CONTEXT_OK`；后续 9 calls 反事实少重放 142,960；2363 项全量、Ruff、compileall、diff check 通过 |
| A276 | 2026-08-25 | Provider-only Write Receipt Projection | production_verified | 仅在真实 provider assistant 已观察成功写入后压缩下一请求中的 diff 回执；当前批次/失败/读取/验证证据与 durable Session 全保留；trace 分原因统计 token savings | A277/A279 反事实累计少重放 57,409/48,332 history tokens；真实 A279 completed、94 passed、semantic 12/12；2355 项全量、Ruff、compileall、diff check 通过 |
| A247 | 2026-08-11 | Core Coding Capability Sprint：Repo Intelligence V3、Tool/Context Scale V2、A–H Benchmark | production_wired（付费模型/SWE效果仍未知） | AST+多语言低置信词法调用图、完整Symbol/Module/Process/Changed Scope/Impact capsule、共享prewarm/watcher；真实Context压力工具暴露、批量结果总预算；统一Trajectory诊断与真实native Runner/验证/冲突A–H基准 | 1741项全量、Ruff、compileall通过；A–H 8/8，300模块、200工具、41模型/40工具轮；未跑付费Provider/SWE，不声称榜单等价 |
| A238 | 2026-08-11 | Native Runtime 去 Host 化与 Child Session 单一真源 | production_wired（七阶段范围完成；AgentLoop 尚非纯 facade） | 新增 `RunRequest + RunOptions` 原生入口；无 AgentLoop 完成 Model→Tool→Model→Final；Main 单向适配原生 Runner；Runner callback bag 45→0；Child 首次/恢复均以 Session 为 transcript/usage 真源，TaskRecord 删除 messages 与顶层 usage 副本；同步 Tool 与 legacy coding host 明确保留为后续债务 | 1613 项全量、Native/Child/architecture 专项、Ruff、compileall、import smoke、diff check 通过；未跑付费 Provider/SWE，不声称榜单等价 |
| A237 | 2026-08-10 | Session-first Runtime 最终生产边界收口 | production_wired（当前提示词核心架构验收完成；兼容 API/全局工具注册/外部成绩不在此结论内） | ModelExecutionContext、RunnerExecutionContext、LifecycleExecutionContext 接入生产；Runner turn/Model/Lifecycle/异步Tool核心均零host；child/background 使用parent-linked原生Session，task state退出transcript ownership；RuntimeServices删除第二SessionRepository owner | 1605项全量、152项Runtime/architecture专项、Ruff、compile/import、diff check通过；离线6任务peak=3、顺序保持、2.77x；未跑付费Provider/SWE |
| A236 | 2026-08-10 | Session-first Runtime Phase 2C：focused ToolExecutionContext | production_wired（生产异步 Tool Runtime host-free；同步 compatibility 与其他 Runtime 仍待迁移） | 新增 ToolPolicy/Lifecycle/Projection/Execution 四类 run-scoped context 与集中 legacy adapter；ProductionToolPolicy、ResultProjector、async batch/dispatch 不再读取 AgentLoop；Runner 每个 run 惰性构造一个 Tool context，checkpoint/事务/调度/guardrail/result/handoff/snapshot 通过显式能力执行；handoff 后同步刷新 policy identity | 1589项全量、67项Model/Tool/architecture专项、Ruff、compile/import与diff check通过；离线6任务peak=3、顺序保持、2.93x；未跑付费Provider/SWE；同步Tool入口仍保留16类host能力 |
| A235 | 2026-08-10 | Session-first Runtime Phase 2B：focused Context 与 Tool checkpoint ownership | production_wired（Phase E 第一批；Tool/Model/Lifecycle 尚未完全 host-free） | 新增不可变 ContextExecutionContext 与集中 legacy adapter；ProductionContextManager 由 24 处 host 访问降为 0，workspace/budget/token projection/compaction/stamp/trace 成为显式能力；Runner 两条 Tool batch 路径注入 SessionRuntime checkpoint，异步 Tool start/interrupted/finish 不再调用旧 repository | 1582项全量、63项架构专项、Ruff、compile/import、离线并发 smoke 与diff check通过；同步 Tool compatibility 仍保留3处旧checkpoint，Runner/Tool其他host能力继续列为债务 |
| A234 | 2026-08-10 | Session-first Runtime Phase 2A：生产 Session/RunContext ownership | production_wired（Phase 2A；host-free ports 与 child/background 原生 Session 尚未完成） | 新增 Session/SessionStore/SessionRuntime/RunContext；真实 AgentRunner 每次运行打开 Session-owned RunContext，19处显式 checkpoint 全部改走 SessionRuntime，终态 exactly-once 收口；SessionProcessor 稳定变更 sink 标记 Session dirty；旧 JSON 格式增加可选 parent/metadata 且保持兼容；修复 Session↔Core 循环导入与后续 user activation 恢复语义 | 1573项全量、58项架构专项、Ruff、compileall、离线并发 smoke 通过；最大 SCC 21→20，Runner旧SessionRepository checkpoint 19→0；仍有27类 host 私有访问，不宣称总体重构完成 |
| A233 | 2026-08-10 | 统一Runtime最终ownership收口 | architecture_verified | Guardrail/Input/Transition/Lifecycle成为必需Runtime端口；Tool准入、调度、收敛、批次观测和结果投影移出Loop；Runner所有初始化/终止路径走同一Lifecycle；修复精简Harness兼容回归 | 1549项全量、Ruff、compileall、dependency scan通过；未跑付费Provider/SWE，不声称外部分数等同 |
| A232 | 2026-08-10 | 对原始统一Runtime提示词重新逐条验收并修正A231过度完成结论 | architecture_verified | RuntimeServices成为生产必需图；Runner改为经Model/Tool/Context/Session/Verifier端口执行；模型异步取消与Memory recall/learning移出Loop；child由composition构造；SDK真实投影guardrails/handoffs/output_schema；新增跨profile trace parity与原文验收矩阵 | 1535项全量、compileall与Ruff通过；未跑付费Provider/SWE，且不据此声称外部分数等同 |
| A231 | 2026-08-10 | 统一Agent Runtime Phase 5–8生产切换与依赖收口 | superseded（当时把阶段完成误写为架构终态） | Main/child/background/workflow/SDK进入唯一AgentRunner的主体切换成立，但当时RuntimeServices尚未成为生产Runner依赖，SDK也未投影guardrails/handoffs/output_schema | 当时1525项全量、compileall与Ruff通过；这些只能证明回归，不足以证明原提示词逐条完成；由A232补验 |
| A230 | 2026-08-10 | 统一Agent Runtime Phase 3–5基础：Tool/Context/Session/Runner | contract_verified（Main/child最终切换未完成） | 将并发读调度、写屏障、取消收敛、批事务和结果生命周期移入ProductionToolRuntime；上下文预检移入ProductionContextManager；新增FileSessionRepository与真实AgentRunner模型→工具→模型状态机；AgentLoop相关入口只保留兼容facade | 1513项全量、Ruff及Tool/Context依赖边界检查通过；共享Runner用Fake services完成两轮工具链、usage、event、session和终态；未跑付费Provider/SWE |
| A229 | 2026-08-10 | 统一Agent Runtime Phase 2：ModelGateway生产边界 | production_verified（主/子/辅助调用已wired） | 新增唯一ResolvedModelRuntime与ProductionModelGateway，统一逻辑/wire模型、能力/价格快照、client所有权、buffered/stream timeout/cancel/retry/fallback/overflow、usage/cost与错误身份；Main、child、planner/replanner、compaction、memory、verifier、stall、vision均迁入，架构测试禁止Agent Core绕过Gateway | 179项高风险组合、1504项全量、compileall/Ruff和AST边界检查通过；Fake Provider真实Main/child/stream/工具链覆盖，未调用付费Provider或SWE |
| A228 | 2026-08-10 | InfCodeX参考的统一Agent Runtime架构Phase 0–1 | contract_verified（架构基础，尚未wired） | 完成主/子双执行内核源码审计，批准兼容facade迁移；新增不可变Agent/profile/request/result、单Run可变状态owner、事件与Provider/Tool/Context/Session/Memory/Verifier Protocol端口，并冻结现有Main返回形状与child签名 | 30项runtime/core新增契约、76项聚焦、1470项全量、compileall/Ruff和依赖隔离检查通过；未实现AgentRunner、未改变生产入口、未调用付费Provider/SWE |
| A227 | 2026-08-10 | InfCodeX Main Agent Sidecar Verifier生产闭环 | trace_verified（本阶段完成） | 源码转译通用LLM Judge、24消息第三方上下文、真实文件修改证据、FEATURE_196 gate、强制单一verdict工具、accept/revise/blocked、15秒/取消/fail-open、同模型默认与成对环境覆盖；异步stop-hook按sidecar-first进入统一CLI/HTTP/SWE构造链 | 28项Sidecar/StopHook聚焦、1440项全量、compileall与diff check通过；真实离线Agent完成Main请求→隔离Verifier请求→accept并生成gate/start/finish trace；未调用付费Provider或SWE批跑 |
| A226 | 2026-08-10 | Agent Core生产链源码级纠偏 | trace_verified（本阶段完成） | generation终态与risk、InfCode即时三连权限门、InfCodeX窗口L1+异步L2 one-cycle nudge、16消息transcript、5秒fail-open、run/compaction reset、strict有界final blocker、默认stop consumer和可审计run_end runtime已组装为一条生产链 | 新行为均先红后绿；205项核心组合、1417项全量测试、py_compile、Ruff、diff check通过；provider-free真实Agent完成read→edit→verify→diff；未调用付费Provider或SWE批跑 |
| A225 | 2026-08-10 | A224后20题真实Trace审计与安全续跑上限 | 已完成诊断，待修复 | 不重跑旧题，以新源码指纹建立20题续片；新增只按完整持久化新结果计数的`--max-new-instances`；确认IPC/terminal改善并定位Bash绕过调查门、代际验证与风险归因等问题 | 20条持久结果；18/18 Agent实例有patch且trace正常结束；2个Git/TLS setup失败；80项相关回归通过；未跑官方harness |
| A224 | 2026-08-10 | SWE strict运行器与Agent阶段收敛 | 部分完成，A226纠偏中 | Queue IPC与Bash workdir已闭环；12/20调查预算、terminal和risk只完成了局部机制，真实trace证明Bash可绕过、验证顺序相关且历史错误污染risk | 当时测试通过但不足以证明源码级生产链对齐；以A225真实trace为准 |
| A001 | 2026-07-29 | LSP 语义代码能力 | 已完成第一阶段 | 原生 stdio JSON-RPC 客户端、服务发现、语义导航、诊断和写后自动诊断 | 345 tests passed；真实 BasedPyright 链路通过 |
| A002 | 2026-07-29 | Repo Map / 代码定义地图 | 已完成第一阶段（完成时仅 Python；A005 已扩展） | 跨文件 Python AST 结构索引、查询过滤、增量缓存和输出限制 | 351 tests passed；真实扫描 143 个 Python 文件 |
| A003 | 2026-07-29 | 分层文件与符号相关性排序 | 已完成第一阶段 | exact、prefix、contains、path、fuzzy 分层排序，多词 AND 匹配和稳定 tie-break | 357 tests passed；真实多词查询定位 1 个文件 |
| A004 | 2026-07-29 | Repo Map 与 LSP workspace symbols 联动 | 已完成第一阶段 | 可选语义补充、八类符号过滤、范围安全、排序去重和首次空响应短重试 | 363 tests passed；真实 BasedPyright 返回 LSPClient |
| A005 | 2026-07-29 | 多语言结构地图 | 已完成第一阶段 | Python AST 加十个非 Python 语言族的保守声明提取，共享缓存、排序和相关 LSP probe | 376 tests passed；真实定位 InfCode workspaceSymbol |
| A006 | 2026-07-30 | 三级记忆与会话压缩复核 | 已完成第一阶段 | 持久化 scratchpad/todo、workspace+session 隔离、anchored summary 和近期完整回合保留 | 382 tests passed；新进程恢复与双次压缩测试通过 |
| A007 | 2026-07-30 | 并行工具调用与副作用屏障 | 已完成第一阶段 | 显式 read/serial/write 效果元数据、连续只读段并行、写与状态工具顺序屏障、动态写工具权限/事务隔离 | 387 tests passed；6 个离线任务、并发上限 3 时约 2.95× |
| A008 | 2026-07-30 | 保守恢复与重复工具调用防线 | 已完成第一阶段 | 连续同名同参调用规范化计数、默认第三次（可配置）dispatch 前阻断、最小改动恢复诊断 | 394 tests passed；42 项恢复/Loop 定向测试通过 |
| A009 | 2026-07-30 | InfCode 验证行为复核与分层验证增强 | 已完成第一阶段 | 确认 InfCode 依赖提示、Bash 结果和写后诊断；在 NZ-Coder 既有 gate 上增加三层计划、失败目标延续和证据代际 | 491 tests passed；235 项聚焦回归通过 |
| A010 | 2026-07-31 | 结构化用户提问工具 | 已完成第一阶段 | 1–4 个问题、2–5 个选项、单选/多选/自定义回答、会话级回调隔离和无头快速失败 | 508 tests passed；128 项 question/Loop/权限/子 Agent 聚焦回归通过 |
| A011 | 2026-07-31 | Plan/Build 模式闭环 | 已完成第一阶段 | 模型可请求进入只读规划、专属计划文件、计划审批、审批期间编辑检测和批次后 Build 解锁 | 517 tests passed；107 项 Plan/Question/Loop/权限聚焦回归通过 |
| A012 | 2026-07-31 | 普通修改的 patch 风险复核与保守重规划 | 已完成第一阶段 | 基于 ChangeTracker 当前快照识别公开符号删除、签名变化、过宽和越界修改；按 patch 指纹一次性反馈并接入可选 replan/reviewer | 533 tests passed；156 项影响分析/变更快照/验证计划/Loop/Reviewer 聚焦回归通过 |
| A013 | 2026-08-01 | 调度与恢复可观测性 | 已完成第一阶段 | 工具调用耗时与 queue wait、批次/分段并发峰值、屏障 drain 等待、doom-loop streak reset 事件和 run 聚合摘要 | 538 tests passed；61 项 Observability/Recovery/Scheduler/Loop 聚焦回归通过 |
| A014 | 2026-08-01 | 实例级运行时上下文隔离 | 已完成第一阶段 | ContextVar 运行参数与 broad-test guard、主/子 Agent scope、Dodo/SWE/evaluation 入口迁移，生产代码不再写模块级 config | 544 tests passed；101 项 Context/Loop/Dodo/SWE/Scheduler 聚焦回归通过 |
| A015 | 2026-08-01 | Provider capability registry | 已完成第一阶段 | immutable 模型能力记录、常见 family 规则、窗口/输出预算、prompt family、tools/stream/reasoning/temperature 与 GPT-5 请求字段策略 | 556 tests passed；80 项 Provider/Context/Loop 聚焦回归通过 |
| A016 | 2026-08-01 | MCP 本地 stdio 工具协议 | 已完成第一阶段 | 严格配置、手写 JSON-RPC 生命周期、工具发现/调用、ContextVar 动态注册、effect/权限映射、外部写入非事务边界与进程组清理 | 579 tests passed；88 项 MCP/权限/调度/Loop 聚焦回归通过 |
| A017 | 2026-08-01 | 原生 Session 事件协议与 Dodo 核心解耦 | 已完成第一阶段 | 实例级有序 EventBus、稳定事件 envelope、过滤/回放/有界队列、ContextVar 发布、SSE framing，以及 Dodo 配置/trace/memory/default CLI 从核心退出 | 586 tests passed；97 项 Session/CLI/Memory/Dodo/Loop/MCP 聚焦回归通过 |
| A018 | 2026-08-01 | 本地 HTTP Session service 与薄客户端 | 已完成第一阶段 | loopback-only Bearer API、Session CRUD、workspace 串行 run、提交屏障事件、SSE replay/live/heartbeat、无头权限拒绝、共享 MemoryManager 锁、代理绕过和 `nz-coder serve` | 595 tests passed；86 项 HTTP/权限/Event/CLI/Loop/Memory/Dodo 聚焦回归通过 |
| A019 | 2026-08-01 | HTTP permission/question 交互闭环 | 已完成第一阶段 | Session pending registry、once/always/reject、question reply/reject、原子生命周期事件、超时与 abort 竞态门 | 605 tests passed；121 项 HTTP/权限/Question/Plan/Event/CLI/Loop/Memory/Dodo 聚焦回归通过 |
| A020 | 2026-08-02 | HTTP workspace routing 与持久恢复 | 已完成第一阶段 | 启动者登记 workspace、稳定选择器 ID、Session 固定路由、非重叠 workspace 运行锁、prompt 状态隔离、重启扫描与 dormant 懒恢复 | 612 tests passed；128 项 HTTP/权限/Question/Plan/Event/CLI/Loop/Memory/Dodo 聚焦回归通过 |
| A021 | 2026-08-02 | HTTP message part 与 SSE 游标恢复 | 已完成第一阶段 | text part delta/update/remove、abort attempt retirement、SSE event ID、Last-Event-ID 严格续传、410 过期、有限自动重连与跨服务重启的连续安全 JSONL tail | 624 tests passed；140 项 HTTP/权限/Question/Plan/Event/CLI/Loop/Memory/Dodo 聚焦回归通过 |
| A022 | 2026-08-02 | 持久 message identity 与原子 idle snapshot | 已完成第一阶段 | 向后兼容的持久 message/text-part metadata、InfCode-style WithParts projection、旧会话确定性迁移、idle snapshot checkpoint 与 strict-after resync | 633 tests passed；155 项 HTTP/权限/Question/Plan/Message/Context/Event/CLI/Loop/Memory/Dodo 聚焦回归通过 |
| A023 | 2026-08-02 | HTTP 断流恢复与崩溃状态闭环 | 已完成 | 慢订阅者显式 gap、snapshot 自动重同步、原子 run 状态落盘、重启 interrupted 语义和 snapshot→SSE→settled 端到端闭环；HTTP 范围冻结 | 642 tests passed；207 项聚焦回归通过；53 项 Event/HTTP 定向测试通过 |
| A024 | 2026-08-02 | MCP Session 生命周期与动态能力刷新 | 已完成第二阶段 | Agent-owned stdio runtime、并行/后台启动、connect/disconnect/reconnect、tools/prompts/resources 缓存、list-changed 与 live tool provider | 656 tests passed；161 项聚焦回归通过；33 项 MCP 定向测试通过 |
| A025 | 2026-08-02 | Provider 精确模型目录与 reasoning variants | 已完成第二阶段 | workspace 内精确 provider/model 记录、variant 选择、三类 adapter 参数映射、命名 OpenAI-compatible provider 与 Session capability snapshot | 683 tests passed；140 项聚焦回归通过；56 项 Provider 定向测试通过 |
| A026 | 2026-08-02 | MCP Streamable HTTP 远程传输 | 已完成第三阶段（本阶段未含 OAuth；核心 OAuth 已由 A027 补齐） | stdlib Streamable HTTP、JSON/SSE 响应、GET 通知流、Session-ID 复用/DELETE、显式环境凭据、HTTPS/重定向/代理/大小边界 | 61 项 MCP 聚焦测试通过；排除 1 个既有 Dodo 收集故障后 707 tests passed |
| A027 | 2026-08-02 | MCP OAuth 授权生命周期 | 已完成第一阶段 | protected-resource/authorization-server discovery、动态注册、PKCE/state loopback callback、URL-bound 0600 store、refresh single-flight、needs_auth 与 CLI auth/status/logout | 13 项 OAuth 定向测试、84 项 MCP/CLI 聚焦测试通过；完整回归 724 tests passed |
| A028 | 2026-08-03 | OpenAI Responses/Codex 原生 Provider | 已完成第一阶段 | 独立 Responses adapter、Chat history→input items、function call/result round-trip、SSE 事件归一化、加密 reasoning replay、专属凭据与显式 provider 路由 | 7 项定向、129 项聚焦测试通过；完整回归 731 tests passed |
| A029 | 2026-08-03 | workspace 持久增量代码索引 | 已完成第一阶段 | workspace 隔离 SQLite symbol/reference cache、重启复用、Repo Map 共用索引、事务提交后增量替换/删除及精确 Python 引用工具 | 7 项索引测试、105 项聚焦测试通过；完整回归 738 tests passed |
| A030 | 2026-08-03 | 写子 Agent 后台并行编排 | 已完成第一阶段 | Session-owned 后台任务组、20 任务/4 并发上限、非重叠路径 claim、Git/文件快照隔离、查询/取消/中断恢复、父 Agent 精确审查与基线冲突后显式应用 | 9 项编排测试、121 项聚焦测试通过；完整回归 747 tests passed |
| A031 | 2026-08-03 | MCP 分层配置、信任与 live reconcile | 已完成第四阶段 | user/project/environment 配置覆盖、项目命令 SHA-256 用户信任、CLI list/trust/untrust、运行中增删改与非法配置保活、Streamable HTTP→legacy SSE fallback | 7 项配置/降级测试、185 项聚焦测试通过；完整回归 754 tests passed |
| A032 | 2026-08-03 | Provider 模型发现、缓存与 workspace picker | 已完成第一阶段 | OpenAI-compatible/Responses、Anthropic、Gemini 显式模型发现，有界分页、0600 无凭据缓存、离线能力展示、workspace 选择/variant 持久化并驱动 AgentLoop | 9 项定向、114 项聚焦测试通过；完整回归 763 tests passed |
| A033 | 2026-08-03 | models.dev 精确能力目录 | 已完成第一阶段 | models.dev-compatible 显式 sync、5 分钟 freshness、跨进程锁、健康快照保留、支持 Provider 归一化，以及 registry→本地 exact catalog→环境 override 的 capability 合并 | 10 项定向、124 项聚焦测试通过；完整回归 773 tests passed |
| A034 | 2026-08-03 | Dodo 平行架构物理收敛 | 已完成 | 证明 legacy Dodo/PySide 生产 caller graph 为空，保留已内化的 core memory/event/HTTP/background-agent/trace 能力，删除 39 个平行控制面、worker、客户端、专属测试和安装文档文件 | 新增 1 项架构防回归；核心聚焦 75 passed；移除 44 项旧产品测试后完整回归 730 passed |
| A035 | 2026-08-03 | 统一扩展描述与状态 Contract | 已完成第一阶段 | immutable ExtensionDescriptor 统一 Skill/Hook/optional tool pack/MCP 的 identity、source、scope、trust、status、capabilities、effects、permissions 和 lifecycle；新增 secret-free CLI 与来源故障隔离 | 10 项定向、100 项聚焦测试通过；完整回归 740 passed |
| A036 | 2026-08-03 | 当前差距再审计与 release baseline | 已完成 | 将 A028 时点旧矩阵标成历史快照，按 frozen/deferred/interoperability/consumer-driven 重建当前边界，并同步 README、architecture 和 release claim | 5 项 reader checks、4 个离线 CLI 冒烟；完整回归 745 passed |
| A037 | 2026-08-03 | prompt_toolkit 终端交互基础 | 已完成第一阶段 | async 多行编辑、私有持久历史、slash/session/model/file 补全、动态状态栏、非 TTY 回退和同 Session 模型热切换/失败回滚 | 69 项聚焦、真实 PTY 通过；完整回归 756 passed |
| A038 | 2026-08-03 | Session-event 驱动的结构化终端运行视图 | 已完成第一阶段 | tool started/completed lifecycle、category/summary/status/duration 卡片、ANSI/control 清理、run settlement、changed-file 摘要、Permission/Question 卡片和错误后继续 REPL | 102 项聚焦、真实终端卡片通过；完整回归 761 passed |
| A039 | 2026-08-03 | CLI Session timeline 与安全会话 fork | 已完成第一阶段 | 隐藏 synthetic diagnostics 的 user-turn timeline、active/saved Session metadata 表、完整回合 deep-copy fork、同 workspace 明示边界和创建失败回滚 | 39 项聚焦、真实 PTY 表格通过；完整回归 769 passed |
| A040 | 2026-08-03 | CLI 异步键盘选择与状态迁移 | 已完成第一阶段 | 单一 awaited radio selector、sync/async 命令兼容分发、Session/model/fork 三类选择，以及既有安全 owner replacement 生命周期复用 | 36 项聚焦、真实 PTY selector 通过；完整回归 776 passed |
| A041 | 2026-08-03 | CLI fuzzy selector 与单 Enter 选择 | 已完成第一阶段 | 自研 value/label selector、确定性 fuzzy 排序、实时输入过滤、循环/翻页导航、单 Enter 返回、Esc 取消和有界可视窗口 | 42 项聚焦、真实 PTY fuzzy/Enter 通过；完整回归 782 passed |
| A042 | 2026-08-03 | CLI 交互完整收口 | 已完成 | Permission once/always/reject、Question 单选/多选/custom/dismiss、阻塞工具 worker 化、线程安全 async bridge、owner 替换后自动重绑，且 HTTP/sync 接口保持兼容 | 121 项交互/调度/HTTP 聚焦、真实 PTY 工具线程→selector 闭环；完整回归 793 passed |
| A043 | 2026-08-03 | 终端产品首次启动与 wheel 发布收口 | 已完成本地发布阶段 | workspace `.env` 且 shell 优先、Provider-specific credential、0600 non-overwrite init、secret-free offline doctor、package-owned bundled skill、source-external wheel smoke 与明确证据矩阵 | 45 项发布/CLI/extension 聚焦、真实 wheel 安装通过；完整回归 802 passed |
| A044 | 2026-08-03 | 真实使用反馈驱动的 CLI 内联输入框 | 已完成 | 移除占满终端高度的固定 bottom toolbar，增加随终端宽度适配的内联 composer 边框与状态标题；空 Enter 保持编辑器而不堆叠提示符，保留多行、补全和历史 | 38 项 CLI 聚焦、真实 PTY 输入框通过；完整回归 803 passed |
| A045 | 2026-08-03 | InfCode-style slash 菜单与模型/权限 picker 链路 | 已完成第一阶段 | 核对 InfCode Autocomplete→CommandDialog→DialogModel 行为，显式触发行首 slash menu、别名投影、Enter 直接执行；`/models` 打开/按需发现模型，`/mode` 打开带风险说明的权限选择器 | 41 项 CLI 聚焦、受控 VT slash render 通过；完整回归 806 passed |
| A046 | 2026-08-03 | 真实 Ctrl+C 故障修复与全能力证据再审计 | 已完成本轮 P0 修复 | Ctrl+C 返回同一 REPL；线程工具取消先收口再回滚；单只读工具不阻塞事件循环；回调异常不悬挂事务；异步 slash 取消不退出进程；重新区分可用闭环、部分对齐和外部证据缺口 | 66 项取消/CLI/上下文聚焦、121 项 LSP/MCP/HTTP、71 项 Provider；814 项分组三段全量回归通过 |
| A047 | 2026-08-03 | 真实代码审查死循环与上下文证据保留 | 部分保留，A049 已纠偏 | 保留模型预算裁剪、最近两个真实用户回合保护和 synthetic message 语义；撤回未经 InfCode 佐证的跨调用 read episode/语义失败熔断 | A047 历史验证保留；当前基线见 A049 |
| A048 | 2026-08-03 | InfCode-style 终端产品控制面收口 | 已完成当前终端范围 | 注册表驱动的 Ctrl+P 分类命令面板、leader/编辑器/粘贴快捷键、主题与鼠标、工具详情、最近/收藏模型、F2 轮换、掩码 Provider 连接、一次性安全附件和粘贴卡片全部进入同一生产 REPL | 69 项终端聚焦测试；真实 PTY Ctrl+P/theme/tool-details/attachment 闭环；完整回归 838 passed |
| A049 | 2026-08-03 | InfCode 源码级 step-limit、doom-loop 与工具投影纠偏 | 已完成 | 最后 step 追加 InfCode `MAX_STEPS` 文本收尾；doom-loop 只比较连续三次同名同参并接入 once/always/reject 权限；Read/Search 单行、Bash 输出保留块 | 78 项聚焦测试；真实 DeepSeek 两 step PTY 收尾；完整回归 843 passed |
| A050 | 2026-08-03 | InfCode 本地终端命令面对齐 | 已完成本地后端范围 | 补齐 `/rename`、`/copy`、`/export`、`/skills`、`/mcps`、`/variants`、`/editor`、`/exit`；Session title 持久化、Markdown transcript、OSC 52/原生剪贴板和 workspace-safe 原子导出进入同一 REPL | 102 项终端聚焦测试；真实 PTY 命令链；完整回归 849 passed |
| A051 | 2026-08-03 | InfCode Ctrl+C 清空与双击退出合同 | 已完成 | 有内容时 Ctrl+C 清空 composer；空输入第一次显示明确提示，一秒内第二次退出；运行中 Ctrl+C 仍只取消当前 Agent；fallback reader 同步相同行为 | 58 项终端聚焦测试；真实 PTY 双 Ctrl+C 退出码 0；完整回归 854 passed |
| A052 | 2026-08-03 | InfCode 核心结束条件与模型 Trace 对齐 | 已完成 | 非 tool-call finish 立即结束；默认移除无 InfCode 对应物的 verification/reflection completion gate；Trace 增加模型等待、TTFT、请求增长、重试和子 Agent span，旧 trace 也可回算 | 116 项核心聚焦 + 59 项上下文/Provider 聚焦；真实同题回放无 gate/reflection；完整回归 855 passed |
| A053 | 2026-08-03 | InfCode 上下文压缩、持久指令与记忆边界 | 已完成当前核心范围 | 软阈值只清理、硬阈值才摘要；接入 Provider usage、模型预算 tail/split；加载全局/项目 AGENTS/CLAUDE/rules；修复默认模式记忆提取、synthetic provenance、压缩后稳定游标和静态重复注入 | 145 项聚焦测试；ruff/diff check 通过；完整回归 861 passed |
| A054 | 2026-08-04 | InfCode input-expansion 与 compaction marker 持久化 | 已完成当前生产入口 | `/attach` 与行内 `@file` 真正展开为独立预算内容；自然语言不被裁剪；支持 later-first、single truncate、tombstone/preflight 持久降级；summary 保存唯一 archive/head IDs/tail boundary，tool 保存 compacted time，并投影为 WithParts metadata | 177 项核心/CLI/HTTP 回归 + 51 项终端/扩展聚焦；ruff 通过；完整回归 869 passed |
| A055 | 2026-08-04 | Agent Core step/tool/retry 持久状态机 | 已完成第一阶段 | assistant 请求前持久 step-start；reasoning、tool pending/running/completed/error、retry 与 step-finish 成为真实 Session parts；取消/失败工具确定性收尾；API retry 分类与 Provider Retry-After；按 InfCode 选择逻辑强化模型族 prompt contract | 163 项首轮聚焦、146 项取消/HTTP/Loop 聚焦；ruff 通过；完整回归 875 passed |
| A056 | 2026-08-04 | Agent Core 流式 message/tool-input 持久化 | 已完成当前 Provider 边界 | text、reasoning 和 tool argument delta 在响应完成前更新同一个 durable part；缺失早期 call ID 时按 index 对账；stream 失败、重试和取消结算未完成工具 | 25 项 processor/event 定向、130 项核心聚焦；ruff/compile 通过；完整回归 880 passed |
| A057 | 2026-08-04 | Agent Core step workspace snapshot/revert | 已完成第一阶段 | 每 step 前后保存真实内容寻址 workspace snapshot；message-level revert/unrevert 同步文件与 history；冲突预检、原子回滚、慢仓库上限和无 Git fallback 进入生产 Loop 与 `/undo`/`/redo` | 158 项核心聚焦；真实本仓初次/增量 capture 0.67s/0.21s；ruff/compile 通过；完整回归 889 passed |
| A058 | 2026-08-04 | Agent Core 统一 tool result/control outcome | 已完成第一阶段 | SessionProcessor 统一消费 tool result/error 和拒绝状态并返回 continue/stop；权限拒绝默认终止当前 turn，不再盲目二次请求；普通工具错误仍反馈模型修正；提供 InfCode 实验性 continue-on-deny 开关 | 138 项 Loop/权限/取消/Session/HTTP 聚焦；ruff/compile 通过；完整回归 893 passed |
| A059 | 2026-08-04 | Agent Core reactive context-overflow compact outcome | 已完成主请求恢复链 | 区分普通 400 与 Provider context overflow；stream/non-stream 均产生 typed compaction outcome；SessionProcessor 返回 compact，持久降级 input expansion、生成 overflow summary 并恢复 turn；三次 guard 防止循环 | 82 项定向、196 项核心/Provider/HTTP 聚焦；ruff/compile 通过；完整回归 897 passed |
| A060 | 2026-08-04 | Agent Core Question result 与 Provider tool metadata | 已完成当前真实 producer 范围 | Question answer/dismiss 以 str-compatible ToolOutput 产生 title/answers/dismissed metadata；dismiss 作为 completed result 继续模型；Responses/Gemini tool-call metadata 从 delta 持久到 ToolPart/Session projection | 157 项交互/Session/MCP/Loop 聚焦；ruff/compile 通过；完整回归 899 passed |
| A061 | 2026-08-04 | Agent Core snapshot PatchPart 与 Session summary diff | 已完成当前核心范围 | step start/finish 内容寻址快照生成非空 PatchPart；按 user turn 保存轻量 diff，按 Session 保存有界完整 patch/增删统计；压缩、磁盘 artifact、timeline、idle snapshot 和 `/session/:id/diff` 共享同一事实源 | 174 项聚焦；ruff/compile/diff check 通过；完整回归 903 passed |
| A062 | 2026-08-04 | Compaction 摘要请求 payload/context overflow 恢复 | 已完成当前核心范围 | 摘要请求溢出后先持久裁剪旧工具输出和 tagged expansion；仅 payload 真实缩小时单次重试；仍失败时按 oversized turn/aggregate head 安全边界写占位 summary，否则保留显式错误 | 187 项聚焦；ruff/compile/diff check 通过；完整回归 909 passed |
| A063 | 2026-08-04 | Agent Core 单一 compaction-attempt owner | 已完成 | usage/request-estimate pre-send compact 与 reactive Provider overflow 共用一次 user run 内三次 guard；第 4 次摘要前持久 exhaustion；manual compact 独立；compaction 时间边界防止 tail 旧 usage 重触发 | 161 项聚焦；ruff/compile/diff check 通过；完整回归 912 passed |
| A064 | 2026-08-04 | Agent Core 运行中工具 metadata 与 Bash progress | 已完成当前 producer/consumer 范围 | execution-local `report_tool_metadata` 对齐 `ctx.metadata()`；并行 call ID 隔离；Bash 增量合并 stdout/stderr，running ToolPart 经 Session checkpoint 与 SSE 更新，完成态保存 title/output/exit/workdir/truncated | 18 项故障边界与 15 项最终聚焦；ruff/compile/diff check 通过；完整回归 916 passed |
| A065 | 2026-08-04 | Agent Core durable QuestionPart 生命周期 | 已完成当前交互范围 | question 工具生成唯一 request ID，并与 HTTP pending broker、QuestionPart 共用；answer 生成 completed card+QuestionSummaryPart，dismiss/cancel/timeout 生成 terminated；崩溃恢复结算幽灵 pending | 38 项定向、151 项核心聚焦；ruff/compile/diff check 通过；完整回归 920 passed |
| A066 | 2026-08-04 | Agent Core 图片 FilePart 与下一轮 Provider replay | 已完成当前图片输入范围 | Read 与 MCP image/resource blob 生成有界 data-URL FilePart；attachment 只存 completed ToolPart，Session 恢复后按 call ID 投影；vision capability 决定是否重放，OpenAI/Responses、Anthropic、Gemini 分别生成合法多模态请求 | 83 项首轮与 116 项最终聚焦；ruff/compile/diff check 通过；完整回归 931 passed |
| A067 | 2026-08-04 | InfCode WebFetch 文本/图片生产链 | 已完成当前标准库范围 | 新增 HTTP(S) webfetch；30 秒默认/120 秒上限、5 MB 声明/流式/解压边界、HTML→Markdown/Text/HTML、gzip/deflate、安全 redirect 与图片 ToolOutput；复用 A066 attachment replay | 122 项聚焦；ruff/compile/diff check 通过；完整回归 937 passed |
| A068 | 2026-08-04 | 用户原生图片 FilePart | 已完成终端入口与视觉 Provider 范围 | `/attach`/`@file` 提交时按签名分流：文本保持 input-expansion，图片成为 user durable FilePart；Session 恢复后按 capability 进入原 user turn，四 Provider wire shape 复用 A066；非视觉模型保留明确路径占位 | 97 项聚焦；ruff/compile/diff check 通过；完整回归 942 passed |
| A069 | 2026-08-04 | 非视觉模型 image-describe preflight | 已完成用户图片生产链 | 主模型请求前用独立视觉模型逐图描述；running/completed/error/interrupted 状态写入当前 assistant TextPart，按源 user/part identity 幂等恢复，并把 terminal XML 描述回填原 user turn；视觉模型仍消费原图 | 102 项聚焦；ruff/compile/diff check 通过；完整回归 949 passed |
| A070 | 2026-08-04 | 非视觉模型 Read 图片描述 | 已完成 Read 工具生产链 | `read_file` 图片结果在 ToolPart 完成前逐图描述；原输出追加 XML hint，`metadata.imageDescribe` 与附件共同持久化；视觉模型跳过，失败逐项降级，取消先保存原 Read 成功结果再停止 turn | 99 项聚焦；ruff/compile/diff check 通过；完整回归 955 passed |
| A071 | 2026-08-04 | PDF/DOCX document-read preflight | 已完成终端 user-turn 生产链 | PDF/DOCX 成为 workspace-relative durable FilePart；主请求前转换为 assistant-owned document_read TextPart，按 source identity 幂等恢复并回填原 user turn；DOCX 标准库解析、PDF 可选系统转换、sidecar cache 与可结算取消闭环 | 111 项聚焦；ruff/compile/diff check 通过；真实 DOCX/PDF smoke；完整回归 966 passed |
| A072 | 2026-08-04 | Read tool PDF/DOCX pagination | 已完成当前本地工具范围 | `read_file` 在 UTF-8 解码前分流 PDF/DOCX；复用 document converter，支持 PDF pages、转换后 offset/limit、20 页上限、超长 PDF 显式分页、XML output/metadata 与页范围隔离 sidecar | 146 项组合回归；ruff/compile 通过；完整回归 972 passed |
| A073 | 2026-08-04 | Read tool text/directory core parity | 已完成本地核心分支 | 普通文本默认 2000 行、50 KiB UTF-8 byte cap、2000 字符单行 cap、严格 offset/continuation、binary/BOM/legacy decode；目录单层排序分页、missing suggestions、ToolOutput metadata 与有界异步 LSP warm | 129 项组合回归；ruff/compile/diff check 通过；完整回归 981 passed |
| A074 | 2026-08-04 | Instruction source/budget/injection parity | 已完成当前 root/rules运行链 | 保持当前 InfCode nested resolve禁用；区分 per-file/total truncation/omission，UTF-8安全预算、tracked/private标签、嵌套 reminder转义；规则独立前置首 user，无 user时回退 system | 180 项组合回归；ruff/compile/diff check 通过；完整回归 988 passed |
| A075 | 2026-08-04 | Agent Core cooperative tool cancellation | 已完成调度基础与 Bash/document consumers | 每个异步 tool call 获得隔离 cancel event；取消先通知 handler再等待 worker收口；Bash终止进程组，PDF转换终止 `pdfinfo`/`pdftotext`，ToolPart统一 interrupted且不留下迟到缓存 | 38 项聚焦；ruff/compile通过；完整回归 993 passed |
| A076 | 2026-08-04 | Grep/Glob cooperative abort consumer | 已完成搜索取消链路 | `grep_search`的系统进程、Python逐行fallback与`glob_search`逐路径遍历都消费per-call event；取消终止进程并等待收口，不返回部分搜索结果 | 67 项聚焦；ruff/compile/diff check通过；完整回归 998 passed |
| A077 | 2026-08-04 | Task/subagent parent-cancel propagation | 已完成前台/后台子Agent取消链 | 当前tool event成为child run唯一cancel owner；异步wrapper主动signal并settle；nested tools继承；活动Provider请求close/poll；取消持久化child状态并回滚未提交写 | 110项组合回归；ruff/compile/diff check通过；完整回归1001 passed |
| A078 | 2026-08-04 | Skill content/files/abort producer | 已完成filesystem skill加载链 | `load_skill`返回正文、base file URI、最多10个资源路径及title/metadata；正文和逐路径采样消费per-call event；保持三级优先级与allowed_tools提示 | 163项组合回归；ruff/compile/diff check通过；完整回归1004 passed |
| A079 | 2026-08-05 | Instruction file enabled state 与控制面 | 已完成 root file CRUD/runtime闭环 | 对齐 global/project `AGENTS.md`/`CLAUDE.md` list/create/enable/delete；原子0600状态、默认启用、损坏告警；loader每次请求过滤；HTTP routes与client共用核心API | 52项Instruction/HTTP聚焦；ruff/compile/diff check通过；完整回归1010 passed |
| A080 | 2026-08-05 | GlobTool → Ripgrep.files 结果语义 | 已完成当前workspace文件搜索链 | `glob_search`只返回文件；支持path/brace/absolute-in-workspace；系统rg有界stream、30秒/取消收口；先取101再按mtime排100；absolute output与ToolOutput metadata | 54项Search/Smoke聚焦；真实rg与本仓smoke；静态检查通过；完整回归1016 passed |
| A081 | 2026-08-05 | GrepTool → Ripgrep.search JSON/partial | 已完成当前content search主链 | 默认返回InfCode式matching lines；严格JSON match/submatch解码、code 0/1/2 partial、file/dir path、mtime排序、100行/2000字符与ToolOutput；旧files/count/context共用rows | 64项Grep/Search/Smoke聚焦；真实本仓rg smoke；静态检查通过；完整回归1026 passed |
| A082 | 2026-08-08 | Shared Ripgrep.files → Skill sample | 已完成当前filesystem skill资源采样链 | 将A080 files producer提升到runtime共享层；Glob与Skill共用rg进程、timeout、取消、bounded stream和fallback；Skill按hidden=true/follow=false、过滤SKILL.md后take10 | 73项Ripgrep/Skill/Search/Smoke聚焦；真实rg hidden/ignore/.git语义；静态检查通过；完整回归1032 passed |
| A083 | 2026-08-08 | Shared Ripgrep.search + scoped process | 已完成当前rg files/search runtime主链 | JSON search下沉runtime；files/search共用唯一process/queue/deadline/cancel/settlement owner；严格验证begin/match/end/summary完整schema与code 0/1/2 | 77项Ripgrep/Grep/Search/Skill/Smoke聚焦；真实rg默认grep；静态检查通过；完整回归1036 passed |
| A084 | 2026-08-08 | Provider stream内工具执行主链 | 已完成当前本地工具stream链 | tool-call闭合后由Provider worker桥接async ToolExecutor；pending→running→result/error→finish-step属于同一stream生命周期；继续消费尾usage，副作用后stream错误禁止重试 | 100项Agent/Session/取消聚焦，121项Provider/上下文/附件聚焦；静态检查通过；完整回归1039 passed |
| A085 | 2026-08-08 | 可安装 Provider adapter 运行链 | 已完成已安装Python adapter范围 | `nz_coder.providers` entry point只做无执行发现；明确选择后才导入，校验API版本与ModelProvider contract，并由workspace selection真实驱动AgentLoop/client | 9项adapter定向、133项Provider/模型/上下文聚焦；ruff/diff check通过；分片完整回归1048 passed |
| A086 | 2026-08-08 | 逻辑模型与Provider API模型身份分离 | 已完成registry→Agent请求链 | registry保留logical ID、`api_model_id`、adapter和endpoint；capability/Session使用logical ID，所有主/规划/记忆/子Agent Provider请求使用wire ID | 3项新身份测试、169项聚焦；compile/ruff/diff check通过；分片完整回归1051 passed |
| A087 | 2026-08-08 | 原生Provider finish/usage与输出上限终态 | 已完成Anthropic/Gemini/Responses生产消费链 | 三类adapter归一化finish reason与累计usage；length写ignored警告并阻止不完整工具，error finish进入Session error；StepFinish保存真实tokens | 7项新终态测试、252项核心聚焦；compile/ruff/diff check通过；分片完整回归1058 passed |
| A088 | 2026-08-08 | Think-tag增量reasoning demux | 已完成stream/non-stream与durable Session链 | 逐chunk状态机识别仅位于开头的`<think>/<thinking>`，跨chunk闭合、未闭合flush、正文close-tag清理；正文与ReasoningPart分流且不污染历史 | 9项新demux测试、237项核心聚焦；compile/ruff/diff check通过；分片完整回归1067 passed |
| A089 | 2026-08-08 | Reasoning/cache详细token链 | 已完成usage→Agent→Session链 | 统一提取reasoning、cache read/write；三类原生Provider映射协议字段；LLMResult/trace/assistant usage/StepFinish保留详细token，零值兼容旧schema | 1项新projection及既有协议端到端增强、233项聚焦；静态检查通过；分片完整回归1068 passed |
| A090 | 2026-08-08 | Empty tool-calls终态守卫 | 已完成Session终态不变量 | `tool-calls/tool_calls` finish但无ToolPart时，StepFinish确定性降级stop；真实Agent不重入、不伪造工具生命周期 | 2项新守卫测试、113项核心聚焦；ruff/diff check通过；分片完整回归1070 passed |
| A091 | 2026-08-08 | Session/worktree物理删除生命周期 | 已完成本地Session所有权闭环 | 删除Session时清理精确会话JSON、plan、artifact和其记录的子Agent worktree；Git/copy worktree统一安全回收；HTTP活跃run原子拒绝，CLI双确认 | 4项生命周期测试及HTTP/终端增强；119项定向；完整回归1078 passed |
| A092 | 2026-08-08 | running ToolPart终端实时投影 | 已完成滚动REPL当前运行视图 | CLI直接订阅既有`message.part.updated`，以同一瞬态区域显示pending/running工具、耗时和Bash output preview，完成后只落一个最终卡片 | 2项实时投影/状态区测试；119项定向；真实PTY help/exit；完整回归1078 passed |
| A093 | 2026-08-08 | Provider RetryPart终端状态 | 已完成retry→Session→CLI链 | 终端消费durable RetryPart，显示attempt、重试倒计时和有界错误摘要；后续assistant/tool进度或run终态确定性清除，不写入scrollback | 1项新retry投影测试、127项核心聚焦；静态检查通过；完整回归1079 passed |
| A094 | 2026-08-08 | Assistant finish/error结构化消息状态 | 已完成Agent→Session→HTTP/SSE链 | StepFinish同步assistant finish；七类InfCode-style typed error有界校验与legacy迁移；snapshot/恢复投影同一info，`message.updated`实时发布且敏感映射字段脱敏 | 4项新schema/processor/HTTP测试、148项核心聚焦；静态检查通过；完整回归1083 passed |
| A095 | 2026-08-08 | Provider异常→typed AssistantError保真 | 已完成exception→LLMResult→Session链 | Provider异常保留auth/API/unknown身份、status、retryable、headers/body和class/code；stream/non-stream/client/post-tool边界传递，敏感字段统一脱敏 | 2项新exception/真实Agent测试、133项Provider/取消聚焦；静态检查通过；完整回归1085 passed |
| A096 | 2026-08-08 | InfCode-style usage归一化与Assistant cost | 已完成registry/provider→Agent→Session链 | input扣除cache、output扣除reasoning；models.dev价格含cache/over-200K分段，Provider实际账单优先；cost进入trace、Assistant info和StepFinish，未知价格不伪报零 | 7项新增/增强测试、126项聚焦；Ruff/compile通过；完整回归1092 passed |
| A097 | 2026-08-08 | 前台子Agent递归cost传播 | 已完成task child→ToolOutput→父Assistant链 | child state累计归一化usage/cost，resume只回传本次增量；父SessionProcessor串行合并，Assistant含父+子，StepFinish仅含父模型本步费用；修复background终态/result发布竞态 | 3项端到端/生命周期新增、172项聚焦及20次取消竞态重复；完整回归1095 passed |
| A098 | 2026-08-08 | Assistant模型/token身份与Session stats consumer | 已完成Provider→Message→save/load/HTTP→CLI统计链 | 每条Assistant持久provider/model和稳定tokens；stats聚合顶层+child模型/工具/费用，父Assistant总cost只计一次，child模型仍单列，未知请求与后台未归属费用显式区分 | 3项schema/stats/command新增、187项聚焦；静态检查通过；完整回归1098 passed |
| A099 | 2026-08-08 | Assistant turn lineage与完成时间 | 已完成user→assistant→Session/HTTP链 | 新Assistant在请求前绑定最近真实user parent和created，所有finish出口写completed；旧Session按真实user顺序、timestamp/Part时间迁移，synthetic诊断不改变parent | 1项legacy迁移新增及Loop/Processor增强、166项聚焦；静态检查通过；完整回归1099 passed |
| A100 | 2026-08-08 | User创建时间与stats时间事实源 | 已完成User producer→Session/stats链 | 新User统一持久created；旧Session只按证据迁移；stats优先消息时间并修正child Unix时间上限 | 2项新增、128项聚焦；完整回归1102 passed |
| A101 | 2026-08-08 | User Agent/model/variant身份 | 已完成CLI/HTTP/child→Session链 | 每个User turn固化当时Agent、逻辑provider/model/variant，模型切换不改写历史 | schema/真实Loop增强、147项聚焦；完整回归1102 passed |
| A102 | 2026-08-08 | Assistant执行身份与workspace路径 | 已完成Loop→Session/HTTP链 | Assistant固化mode/agent/variant和cwd/root，公开同名字段不可伪造 | schema/真实Loop增强、158项聚焦；完整回归1102 passed |
| A103 | 2026-08-08 | RetryPart typed error/time | 已完成Provider exception→RetryPart链 | 保留message/next兼容字段，同时持久InfCode式attempt/error/time.created | Processor/Loop测试增强、117项聚焦；完整回归1102 passed |
| A104 | 2026-08-08 | Assistant endState终态 | 已完成run finalize→Session/SSE链 | 只给当前User turn最终Assistant写不可覆写的completed/errored/canceled/interrupted，中间step不误标 | 1项新增及Event/Loop增强、145项聚焦；完整回归1102 passed |
| A105 | 2026-08-08 | Session fork身份图与标题语义 | 已完成普通Session fork主链 | fork为全部Message/Part生成新身份并重连parent/source/compaction引用；标题按`fork #N`递增，且不伪造task parentID | 81项聚焦、静态检查；完整回归1105 passed |
| A106 | 2026-08-08 | Session默认标题fallback | 已完成首个真实User→持久Session链 | 默认`New Session`只在首个真实User出现时变为100字符确定性标题；忽略synthetic且不覆盖手工标题 | 80项聚焦、静态检查；完整回归1107 passed |
| A107 | 2026-08-08 | 子Agent统一Message/Part生命周期 | 已完成child Provider/tool→持久state链 | child Assistant拥有parent/identity/path/time/model/usage/cost/endState，tool经过统一pending/running/terminal Part | 66项聚焦、静态检查；完整回归1107 passed |
| A108 | 2026-08-08 | 子Agent Provider错误Assistant owner | 已完成child failure→typed Session链 | 超时、取消和API异常均生成Assistant/StepFinish/typed error/endState，不再只写孤立status | 64项聚焦、静态检查；完整回归1108 passed |
| A109 | 2026-08-08 | 子Agent Provider RetryPart闭环 | 已完成child transient failure→retry→同一Assistant链 | 复用顶层错误分类/Retry-After/指数退避，RetryPart持久且等待可取消，成功不重复Assistant | 65项聚焦、静态检查；完整回归1109 passed |
| A110 | 2026-08-08 | Fork task-child递归克隆与worktree所有权 | 已完成fork→child state/worktree主链 | 重建child身份和task引用；write child复制changed/deleted状态到新受管worktree；活动child拒绝且失败回收 | 67项聚焦、静态检查；完整回归1113 passed |
| A111 | 2026-08-08 | Assistant终态/typed error终端消费 | 已完成Message event→terminal error/footer链 | 消费既有`message.updated`，typed error只渲染一次并给出恢复动作；最终agent/model/time/endState进入终端 | 38项终端聚焦；纳入1118项完整回归 |
| A112 | 2026-08-08 | task child层级实时终端投影 | 已完成child→parent ToolPart→terminal链 | child通过父task metadata持续投影session/status/current tool/title/count；终端显示层级`↳`进度且不建立第二协议 | 64项组合聚焦；纳入1118项完整回归 |
| A113 | 2026-08-08 | wheel真实PTY与Ctrl+C竞态收口 | 已完成已安装产品入口发布门 | 双Ctrl+C不再重建composer；wheel smoke新增真实composer、slash菜单、清空、退出和无traceback检查 | 65项组合；source-external wheel+PTY通过；完整回归1118 passed |
| A114 | 2026-08-08 | task child Session只读导航 | 已完成父Session→child picker→transcript链 | `/subagents [ID]`列出精确owned child并打开完整Message/Part transcript；不替换父Agent/worktree，返回后仍在父会话 | 97项组合；完整回归1120 passed |
| A115 | 2026-08-08 | full-screen transcript/composer第一阶段 | 已完成idle Session view→submission链 | 默认交互CLI以alternate screen显示有界可滚动transcript和固定多行composer，复用补全/history/Ctrl+C；非TTY与注入session不变 | 68项终端聚焦、wheel PTY；纳入1122项完整回归 |
| A116 | 2026-08-08 | 单历史turn详情 | 已完成timeline→message detail consumer | `/message [TURN]`或picker打开一个turn的完整user/Assistant/ToolPart，不改变Session状态 | 61项组合；纳入1122项完整回归 |
| A117 | 2026-08-08 | full-screen自适应sidebar | 已完成Session owners→wide-layout链 | 宽度>120自动显示session/workspace/message/context/changed files；`/sidebar auto|show|hide`持久控制 | 静态/终端组合与wheel PTY；纳入1122项完整回归 |
| A118 | 2026-08-08 | interactive child route | 已完成owned child→resume Provider/tool链 | `/subagent [ID] [PROMPT]`恢复child原agent/tool/path/worktree与settled cancellation，不替换父Agent/history/workspace | 96项组合、Ruff；完整回归1122 passed |
| A119 | 2026-08-08 | full-screen Markdown安全渲染 | 已完成transcript→formatted text链 | Rich Markdown转ANSI formatted text进入viewport；先清除嵌入ANSI/control，保留标题/强调/代码布局 | 2项新增、55项组合、wheel PTY；纳入1125项回归 |
| A120 | 2026-08-08 | sidebar Todo/MCP/LSP状态组件 | 已完成现有runtime owner→sidebar链 | sidebar读取todo、已存在MCP runtime与workspace-scoped LSP client；显示不会启动外部进程 | 1项LSP隔离新增、56项组合；完整回归1125 passed |
| A121 | 2026-08-08 | 单一长期存活终端Application | 已完成CLI启动→输入/运行/弹层→退出链 | interactive TTY只创建一个prompt_toolkit Application；stream/tool/retry/命令进入同一screen，permission/question/model/session picker为原位overlay，运行中Ctrl+C只取消当前Agent | 3项生命周期新增、124项终端组合、Ruff；完整回归1128 passed；source-external wheel PTY断言单次alternate-screen通过 |
| A122 | 2026-08-08 | 长会话虚拟化渲染与sticky scroll | 已完成Message/Part cache→viewport consumer链 | durable Markdown只在revision/宽度变化时生成并分行；UIControl按viewport索引，stream/status为独立有界安全行；PageUp脱离、End恢复bottom follow | 3项性能/安全/scroll新增；10000行×1000更新从7.46s降至0.0055s |
| A123 | 2026-08-08 | Terminal ErrorBoundary与安全降级 | 已完成root task failure→一次reset→Rich fallback链 | Application异常/取消/EOF不再让submission死锁；首次重建同一状态树，二次失败恢复terminal并让console/renderer切换inline | 2项failure/fallback新增；真实PTY resize/连续命令通过 |
| A124 | 2026-08-08 | credential-free首装与Linux Python矩阵 | 已完成wheel→dependency install→init/doctor/TUI链 | 缺ensurepip时回退virtualenv；wheel安装真实解析依赖；清除开发机凭据验证missing-credential同屏提示；Python3.12/3.13通过 | 两版本source-external wheel PTY；两版本完整回归1133 passed |
| A125 | 2026-08-08 | full-screen leader map与复制最新回答 | 已完成keybind→command→typed TextPart→clipboard链 | A121长期Application恢复全部Ctrl+X leader；新增Ctrl+X Y/`/copy-last`，只复制最新Assistant可见TextPart，忽略reasoning/ignored/synthetic | 2项新增及leader真实输入增强；双版本1135 passed、双wheel PTY通过 |
| A126 | 2026-08-08 | 消息组件身份与边界导航 | 已完成Message graph→structured transcript→line anchor链 | transcript不再只是整段Markdown；真实User/Assistant保留稳定message ID和渲染行范围，支持first/last/next/previous/last-user及完整消息块截断 | 与A127合计2项新增、63项聚焦、双版本1137 passed |
| A127 | 2026-08-08 | 鼠标消息详情与ToolPart原位展开 | 已完成Part→projection anchor→mouse consumer链 | 点击User/Assistant在同一Application打开详情；completed ToolPart按hidden/compact/full投影，compact点击只展开该Part的input/output | Ruff、双版本完整回归及source-external wheel PTY通过；未运行Provider/SWE-bench |
| A128 | 2026-08-08 | 详情独立滚动与selection-safe鼠标 | 已完成detail Markdown→独立UIControl/Window链 | 消息详情拥有独立虚拟viewport和Up/Down/Page/Home/End；鼠标拖动不再误触消息/ToolPart，hover只重绘目标Part | 与A129合计4项新增、90项终端组合、双版本1141 passed |
| A129 | 2026-08-08 | workspace可配置消息键位 | 已完成preference→DynamicKeyBindings→当前Application热更新链 | `/keybind`支持list/set/none/default/reset；使用InfCode action名，非法action/key在写盘前拒绝，修改不重建root screen | Ruff、双版本完整回归及安装wheel keybind PTY通过；未运行Provider/SWE-bench |
| A130 | 2026-08-09 | queued follow-up步骤边界接管 | 已完成terminal queue→Agent step boundary→下一请求链 | 运行中提交的新prompt/command在当前Provider流、工具与事务完全结算后中断旧turn，阻止下一次无效LLM请求；相同snapshot跳过空Session patch重建 | 3项新增；Python3.12/3.13各1144 passed；双wheel PTY及真实DeepSeek read→follow-up通过 |
| A131 | 2026-08-09 | InfCodeX 子 Agent 在途消息路由 | 已完成第一阶段 | Session-owned mailbox支持child→sibling、child→worker和有界broadcast；只在settled step边界注入隔离的untrusted synthetic context，并用seen_by阻断转发环 | 纳入241项Agent Core/验证聚焦回归；Ruff通过 |
| A132 | 2026-08-09 | InfCodeX Runner stop-hook与有界reanimate | 已完成第一阶段 | natural-end可由typed hook接受、追加一次继续指令或显式终止；默认2次预算、每run重置、隔离transcript快照和trace事件 | 纳入241项Agent Core/验证聚焦回归；Ruff通过 |
| A133 | 2026-08-09 | 子 Agent lineage/outcome证据归并 | 已完成第一阶段 | task结果携带parent/session/agent/trace/status/files/conflicts/verification；RunEvidence单独保存child outcome，只有apply后才计入父工作区修改证据 | 纳入241项Agent Core/验证聚焦回归；Ruff通过 |
| A134 | 2026-08-09 | InfCodeX声明式Agent handoff Runner | 已完成第一阶段 | AgentSpec/HandoffSpec构成有向无环图；`emit_handoff`在事务结算后切换system role，支持inputFilter、onAgentSwitched、HandoffPart与terminal signal | 146项Agent Core/Memory组合通过；Ruff通过 |
| A135 | 2026-08-09 | append-only Session lineage与崩溃恢复 | 已完成第一阶段 | 0600 JSONL记录run/handoff/terminal/child outcome，sequence+parent链验证、坏中段拒绝、截断尾恢复；未结算run可恢复active Agent | 146项Agent Core/Memory组合通过；Ruff通过 |
| A136 | 2026-08-09 | Agent工具声明、Artifact与Memory outcome receipt | 已完成第一阶段 | 每角色Provider tool schema和dispatch前guardrail同步收窄；file/command/attachment artifact及memory outcome幂等进入lineage；压缩后注入有界recovery seed | 169项聚焦、1163项全量通过；Ruff通过 |
| A137 | 2026-08-09 | InfCodeX `as-tool` handoff | 已完成进程内闭环 | 调用者与被调用Agent使用隔离transcript；自然结束/terminal后恢复调用者system role并注入有provenance的untrusted result；嵌套深度上限8 | 180项聚焦、1164项全量通过；Ruff通过 |
| A138 | 2026-08-09 | `as-tool` Caller frame崩溃恢复 | 已完成当前单进程Session闭环 | Caller stack以0600原子快照持久化；lineage重建active role/depth，截断过期frame并拒绝缺失frame，Helper崩溃后可回到Caller | 纳入186项聚焦、1170项全量通过；Ruff通过 |
| A139 | 2026-08-09 | 每Agent Provider/model/effort与reasoning profile | 已完成核心请求链 | Agent声明真实切换Provider client、wire model、能力预算和模型族prompt；default/max reasoning在replan/reflection revise时一次性升级 | 纳入186项聚焦、1170项全量通过；Ruff通过 |
| A140 | 2026-08-09 | Agent input/output/tool guardrail runtime | 已完成核心闭环 | run-scoped input/output与每调用tool before/after支持allow/rewrite/block/escalate；tool block反馈模型自纠，output启用时只发布审核后流式终态 | 纳入186项聚焦、1170项全量通过；Ruff通过 |
| A141 | 2026-08-09 | Agent runtime统一组装与真实入口收敛 | 已完成主入口闭环 | CLI/HTTP/local eval/Aider/SWE统一由composition owner构造；coding与declared control-plane互斥；terminal summary贯通guardrail、lineage与最终输出 | 107项入口组合、1173项全量通过；Ruff通过 |
| A142 | 2026-08-09 | InfCodeX Agent admission与运行时Invariant Session | 已完成不可信graph闭环 | system capability准入裁剪、typed handle入口、具体Bash二次降权、min-wins轮次、committed mutation/evidence观察、finalOwner终态拒绝进入统一runtime | 33项聚焦、1186项全量通过；Ruff通过 |
| A143 | 2026-08-09 | InfCodeX workflow child structured output | 已完成declared Agent闭环 | output_schema声明校验、fenced JSON提取、Schema子集递归验证、一次无工具repair、result与as-tool caller共用validated payload | 42项聚焦、1195项全量通过；Ruff通过 |
| A144 | 2026-08-09 | InfCodeX typed child workflow result | 已完成三条child结果链统一 | `ChildAgentResult`统一前台task、background manager、apply与as-tool；旧task接入output_schema和一次repair；legacy metadata保持只读兼容 | 101项聚焦、1202项全量通过；Ruff通过 |
| A145 | 2026-08-09 | InfCodeX child routeFacts与语义模型层级 | 已完成child路由观测闭环 | fast/balanced/deep显式解析；write child禁止fast降级；结果持久requested/outcome/source/fallback/model/iterations/tokens/duration | 纳入114项聚焦、1215项全量通过 |
| A146 | 2026-08-09 | InfCodeX workflow evidenceRefs | 已完成spawn→briefing→result链 | file/diff/finding/task_id前置校验、安全解析、未知task拒绝、单项/总量预算、untrusted framing及resume冻结 | 纳入114项聚焦、1215项全量通过 |
| A147 | 2026-08-09 | InfCodeX child postcondition verification | 已完成机器后置条件闭环 | hard/warn、mutation、changed/read path、finalText长度、preparatory拒绝；typed reasons/evidence进入统一结果 | 纳入114项聚焦、1215项全量通过 |
| A148 | 2026-08-09 | InfCodeX bounded verification repair | 已完成同Session一次修复闭环 | hard失败追加synthetic repair、仅增加一次预算、保留Message/Part/usage/route累计；二次失败确定性终结 | 纳入114项聚焦、1215项全量通过 |
| A149 | 2026-08-09 | InfCodeX full result与presentation summary分离 | 已完成后台状态consumer闭环 | full final_text继续供审计/合成；有界excerpt+summary_kind持久化并供status/as-tool消费；所有失败路径同样生成 | 114项聚焦、1215项全量通过；Ruff通过 |
| A150 | 2026-08-09 | InfCodeX WorkflowProcessSnapshot | 已完成background process consumer闭环 | revisioned snapshot统一item/status/count/progress/token/summary；status不再自行拼唯一机器状态 | 128项聚焦、1220项全量通过；Ruff通过 |
| A151 | 2026-08-09 | InfCodeX Workflow append-only events与replay | 已完成本地单进程持久恢复闭环 | queued/started/cancel-requested/terminal/reconciled事件fsync；sequence+parent链、snapshot重建、截断尾恢复、完整损坏拒绝、events cursor consumer | 128项聚焦、1220项全量通过；Ruff通过 |
| A152 | 2026-08-09 | InfCodeX Workflow wait/stop/cleanup终态 | 已完成本地后台控制闭环 | batch wait共享deadline与顺序结果；timeout主动stop+有界settle；stop幂等、terminal event去重；AgentLoop关闭结算未等待child | 24项专项、1225项全量通过；Ruff通过 |
| A153 | 2026-08-09 | InfCodeX Workflow fan-out并发池 | 已完成本地并行调度闭环 | maxAgents生命周期额度与maxConcurrency活跃额度分离；batch admission原子化、失败隔离、稳定结果顺序、terminal-before-release及可重放峰值 | 26项专项、1227项全量通过；Ruff通过 |
| A154 | 2026-08-09 | InfCodeX gated final synthesis | 已完成真实Child合成闭环 | synthesis通过同一Manager启动read-only/deep Child，计入双额度、事件、usage与typed result；只消费完整child结果和rubric | 五阶段9项新增、1236项全量通过 |
| A155 | 2026-08-09 | InfCodeX phase/pipeline/map-reduce | 已完成受限声明式Workflow主链 | named phase、bounded parallel、无全局stage barrier的per-item pipeline、failure-isolated map与gated reduce共用同一后端 | 150项核心组合、1236项全量通过 |
| A156 | 2026-08-09 | InfCodeX workflow quality preflight | 已完成spawn前质量门 | phase/task/rubric/source/concurrency/write scope/literal fanout/maxAgents/final synthesis在发布child前统一拒绝 | 150项核心组合、1236项全量通过 |
| A157 | 2026-08-09 | InfCodeX content-addressed resume | 已完成成功child重放闭环 | canonical input SHA-256+occurrence；0600结果、corrupt/failure miss、prior→current copy-forward；terminal synthesis始终fresh | 150项核心组合、1236项全量通过 |
| A158 | 2026-08-09 | InfCodeX workflow event→SessionEvent/SSE | 已完成现有客户端协议桥接 | fsynced journal为事实源；phase/task/replay/synthesis事件投影为workflow.*并携snapshot，现有HTTP SSE直接消费 | 150项核心组合、1236项全量通过 |
| A159 | 2026-08-09 | InfCodeX sidecar verifier | 已完成独立验证与返工闭环 | fresh read-only/deep Child输出accept/revise/blocked；最多2次返工，协议失败fail-open，blocked阻断发布 | 13项Workflow专项通过 |
| A160 | 2026-08-09 | InfCodeX workflow budget/abort语义 | 已完成运行时控制闭环 | spawn前token budget门、Child终态usage累计、caller abort停止全部active Child、run终态唯一 | 13项Workflow专项通过 |
| A161 | 2026-08-09 | Workflow进程级硬终止边界 | 已完成可选隔离模式 | task级thread/process声明；spawn子进程共享持久state，取消时terminate→grace→kill，默认线程路径兼容 | 2项真实进程专项通过 |
| A162 | 2026-08-09 | InfCodeX workflow outcome→memory control plane | 已完成Session记忆收口 | 完成结果以幂等、有界memory_outcome_digest进入Lineage；只记phase/task/digest/verdict/budget，不复制raw输出 | 22项Workflow/契约组合通过 |
| A163 | 2026-08-09 | InfCodeX workflow parity contract | 已完成机器行为契约 | versioned contract固化终态、失败隔离、cache、verifier、额度、isolation与outcome语义并随结果返回 | 85项核心组合通过；全量1251项通过 |
| A164 | 2026-08-09 | InfCodeX WorkflowScriptManifest | 已完成声明→admission闭环 | 严格name/description/phases/readOnly/planned/max/concurrency/token/patterns；与真实plan、Session额度和sidecar最坏spawn数交叉校验 | 51项Workflow/Manager组合通过 |
| A165 | 2026-08-09 | InfCodeX managed run lifecycle | 已完成pause/resume/stop闭环 | run registry、spawn前pause gate、resume唤醒、stop取消同run活跃Child、终态防completed竞态、500终态retention | 51项Workflow/Manager组合通过 |
| A166 | 2026-08-09 | InfCodeX workflow artifact | 已完成有界持久artifact闭环 | phase结果写0700 run目录/0600 JSON artifact，安全名称、2MiB上限、原子replace及artifact_written事件 | 51项Workflow/Manager组合通过 |
| A167 | 2026-08-09 | InfCodeX workflow log | 已完成持久进度事实闭环 | 1–4000字符结构化log进入同一fsynced workflow journal并桥接SessionEvent/SSE，不新增日志事实源 | 142项核心组合通过 |
| A168 | 2026-08-09 | InfCodeX run graph/cost report | 已完成terminal record闭环 | 每run原子run.json、started/ended/status/artifacts、typed token/turn/status/coverage/wall-clock报告；contract升级1.1 | 1258项全量通过；Ruff通过 |
| A169 | 2026-08-09 | InfCodeX Workflow Capsule contract | 已完成JSON-only Capsule闭环 | nzcoder.workflow/version/api/minVersion/manifest/plan/intent/requires/provenance严格验证；拒绝source和未知顶层字段 | 6项Capsule专项通过 |
| A170 | 2026-08-09 | InfCodeX Capsule requirement preflight | 已完成执行前能力门 | semver、Git/worktree、tools/MCP/skills/model tiers逐项error/warning；不启动MCP或加载代码 | 58项Workflow组合通过 |
| A171 | 2026-08-09 | InfCodeX saved workflow discovery | 已完成project/personal发现闭环 | 仅普通.workflow.json、1MiB边界、0600原子保存、symlink忽略、project同名覆盖personal、读写工具effect分离 | 58项Workflow组合通过 |
| A172 | 2026-08-09 | InfCodeX saved workflow execution | 已完成发现→预检→plan admission→runtime闭环 | workflow_run支持capsule_name/source；来源与preflight进入outcome、run.json和Lineage digest | 149项核心组合通过 |
| A173 | 2026-08-09 | InfCodeX workflow lifecycle result/artifact/retention | 已完成可恢复历史管理 | 安全list/read/artifact；active拒绝归档；全目标预检后移动到私有.trash，避免部分操作与不可恢复删除 | 1264项全量通过；Ruff通过 |
| A174 | 2026-08-09 | Builtin/Saved Workflow统一解析 | 已完成 | builtin优先于project/personal，saved args有界替换，统一preflight/ref | 17项新增专项通过 |
| A175 | 2026-08-09 | 一级嵌套Workflow | 已完成 | `mode=workflow`在effect前展开、递归lint、阶段路径带父前缀，拒绝二级嵌套 | 17项新增专项通过 |
| A176 | 2026-08-09 | 嵌套资源共享 | 已完成 | nested与parent共用Agent cap、并发、token budget、abort、cache、journal和run record | 7-token门禁专项通过 |
| A177 | 2026-08-09 | Trusted builtin registry | 已完成第一阶段 | 数据式内置注册、list/show、不可被saved同名遮蔽 | resolver专项通过 |
| A178 | 2026-08-09 | Parallel Investigation | 已完成 | bounded read-only investigators、结构化finding、fresh synthesis | fan-out专项通过 |
| A179 | 2026-08-09 | Immutable Review Packet | 已完成 | supplied diff单次捕获、UTF-8字节安全分块、area/category分区、hash/0600原子证据 | Unicode/分区专项通过 |
| A180 | 2026-08-09 | Scoped Review | 已完成 | packet→primary→deep verifier→quality gate→synthesis进入同一WorkflowRuntime | 3-Agent端到端专项通过 |
| A181 | 2026-08-09 | Review Quality Gate | 已完成 | refuted丢弃，confirmed/unresolved/unverified保留；缺失结构输出禁止默认通过 | gate专项通过 |
| A182 | 2026-08-09 | Workflow Worktree Sweep | 已完成当前安全范围 | 终态/启动陈旧清扫；changed或无法证明clean时保留；cleanup fail-soft | clean/changed专项通过 |
| A183 | 2026-08-09 | JSON-only Workflow Generator | 已完成六模式 | 六种manifest pattern生成有界inert Capsule，不生成/执行源码 | 6模式schema专项通过 |
| A184 | 2026-08-09 | Workflow historical identity | 已完成 | 安全run ID、terminal record和run dir解析；unsafe/missing不猜测 | Host专项通过 |
| A185 | 2026-08-09 | Display alias与歧义关闭 | 已完成 | 唯一alias可解析；重复run或run/saved/builtin碰撞返回ambiguous | Host专项通过 |
| A186 | 2026-08-09 | Command-only invocation policy | 已完成 | explicit command为suggest；natural language不被Host抢跑 | Host专项通过 |
| A187 | 2026-08-09 | Start outcome turn contract | 已完成 | started/cancelled消费turn，declined/failed不消费 | Host专项通过 |
| A188 | 2026-08-09 | Host resource limit normalization | 已完成 | manifest/host/system min-wins；Agent/并发正整数，非正token表示unbounded | Host专项通过 |
| A189 | 2026-08-09 | Pre-run approval summary | 已完成 | name/description/phases/planned/effective limits/write risk形成稳定摘要 | Host专项通过 |
| A190 | 2026-08-09 | Host ceilings真实执行 | 已完成 | maxAgents effect前拒绝、maxConcurrency实际压池、tokenBudget spawn前门禁 | 执行专项通过 |
| A191 | 2026-08-09 | Display name持久消费链 | 已完成 | alias进入start event、managed snapshot、outcome、run.json和history | 端到端专项通过 |
| A192 | 2026-08-09 | Identity-aware resume | 已完成 | run ID或唯一display alias可seed cache；saved/ambiguous/missing拒绝 | replay专项通过 |
| A193 | 2026-08-09 | Scout-then-author Host API | 已完成当前NZ边界 | 共享prompt要求Agent先查具体文件/子问题，再用现有workflow_run编排；新增workflow_host工具 | 14项新增、95项核心、1296项全量通过 |
| A194 | 2026-08-09 | Approval summary digest | 已完成 | canonical effective summary绑定SHA-256决策身份 | 新增专项通过 |
| A195 | 2026-08-09 | Stale approval fail-closed | 已完成 | digest不符返回failed且零run/零Child | 新增专项通过 |
| A196 | 2026-08-09 | Approval outcome gate | 已完成 | approve/deny/cancel/pending typed outcome与turn语义统一 | 新增专项通过 |
| A197 | 2026-08-09 | Headless approval receipt | 已完成 | 无callback自动执行但显式记录headless-auto receipt | 端到端专项通过 |
| A198 | 2026-08-09 | Terminal run rename | 已完成 | printable alias原子写回run record并发布rename事件 | 新增专项通过 |
| A199 | 2026-08-09 | Saved workflow rename | 已完成 | exact scope、目标冲突拒绝、原子rename | 新增专项通过 |
| A200 | 2026-08-09 | Saved workflow recoverable delete | 已完成 | confirm门后移动私有.trash，不不可逆删除 | 新增专项通过 |
| A201 | 2026-08-09 | Saved workflow replace revision | 已完成 | 新Capsule先验证/限长，旧版本0600保留后原子替换 | 新增专项通过 |
| A202 | 2026-08-09 | Workflow result summary | 已完成 | list/dict/string递归提取有界终态摘要并提供reader | 新增专项通过 |
| A203 | 2026-08-09 | Retention dry-run | 已完成 | 无confirm只返回完整预检candidate且不移动目录 | 新增专项通过 |
| A204 | 2026-08-09 | Active+persisted history union | 已完成 | managed snapshots优先、持久历史去重、统一limit | 新增专项通过 |
| A205 | 2026-08-09 | Generation JSON extraction | 已完成 | plain/fenced/surrounded外层JSON确定性提取 | 新增专项通过 |
| A206 | 2026-08-09 | Typed decline/generate envelope | 已完成 | decline必须reason；generate必须approval summary并产出validated Capsule | 新增专项通过 |
| A207 | 2026-08-09 | Generation timeout contract | 已完成 | explicit seconds→seconds env→legacy ms→120s默认，最大600s | 新增专项通过 |
| A208 | 2026-08-09 | Bounded generation repair | 已完成 | JSON-only修复提示最多2次，超限返回allowed=false | 新增专项通过 |
| A209 | 2026-08-09 | Generation tool consumer | 已完成 | parse/timeout/repair进入workflow_generation只读工具 | 工具专项通过 |
| A210 | 2026-08-09 | Main Agent tool-name repair | 已完成 | persistence/dispatch前唯一case/separator等价修复并trace | 真实Loop专项通过 |
| A211 | 2026-08-09 | Child Agent tool-name repair | 已完成 | Subagent同一规则修复后才持久化与dispatch | 真实Child专项通过 |
| A212 | 2026-08-09 | Tool result structured classification | 已完成 | error/cancel/code统一进入ToolExecutionResult metadata | Executor专项通过 |
| A213 | 2026-08-09 | Provider retry与terminal diagnostics | 已完成 | retry人类标签及COMPLETE/BLOCKED/DECIDE信号进入现有trace | 28项新增、239项核心、1324项全量通过 |
| A214 | 2026-08-09 | Provider Attempt Controller与stream watchdog | 已完成 | idle/hard watchdog、取消轮询、稳定边界判断和单次buffered fallback进入真实stream consumer | 9项最终闭环专项；1333项全量通过，另1项环境敏感测试deselect |
| A215 | 2026-08-09 | Workflow Provider生成编排 | 已完成 | 当前Provider真实生成JSON Capsule；初次+最多两次repair共享单一wall-clock预算 | 同上 |
| A216 | 2026-08-09 | 终端Workflow审批与命令面 | 已完成 | digest绑定的approve/deny/cancel renderer；`/workflow`生成、运行、查看、暂停、恢复、停止 | 真实PTY `/workflow list`通过 |
| A217 | 2026-08-09 | Workflow异步Host SDK | 已完成 | first-started与terminal Future、非阻塞启动、pause/resume/stop handle | 同上 |
| A218 | 2026-08-09 | Workflow跨进程identity恢复 | 已完成 | journal重放run identity；重启后孤儿active run关闭式结算failed | 同上 |
| A219 | 2026-08-09 | Provider/MCP互操作验证入口 | 已完成验证入口 | live操作显式`--confirm-live`；默认dry-run且不消费凭据/额度 | CLI真实dry-run通过；公网未执行 |
| A220 | 2026-08-09 | SWE-bench可复现身份清单 | 已完成验证入口 | 主CLI接入swebench；first-pass生成source/config/instance secret-free manifest | CLI help通过；300实例未执行 |
| A221 | 2026-08-09 | 真实Provider、终端与SWE小样本证据闭环 | 已完成首轮真实证据 | DeepSeek text/tool/stream、真实PTY read_file、官方SWE固定前10题first-pass全部闭环 | 1335项干净环境回归；官方10题6 resolved/4 unresolved/0 errors |
| A222 | 2026-08-09 | SWE-bench Verified严格主榜流程 | 已完成代码闭环，待500题实跑 | Verified 500设为主榜、Lite 300仅冒烟；严格pass@1禁hints/官方测试反馈/答案联网，exact-once恢复、公开轨迹、官方提交包fail-closed | 22项严格契约、203项聚焦与1357项完整回归通过；未运行500题或付费推理 |
| A274 | 2026-08-24 | 顺序编辑合同与既有语义 Oracle | 已完成真实闭环 | DeepSeek mutation description 明确非重叠/append；syntax alias 测试以现有 canonical/numeric 行为为 oracle | 真实 Session 13+1 calls、21 tools；独立 90 passed、semantic 10/10；TP-025 closed |

状态含义：

- “已完成第一阶段”：核心链路已可用且有测试，但仍存在语言覆盖或产品体验差距。
- “已完成”：当前计划范围内与 InfCode 的目标能力已基本对齐。
- “进行中”：已有代码落地，但接口或验证尚未闭环。
- “计划中”：只完成差距分析，尚未修改代码。

最新差距判断见 A049–A085。第 30/39 节保留各自时间点的历史判断；A046–A085 按实际消费者、取消/失败边界和真实运行证据重新定级，不以目录、模块名或测试数量代替能力判断。

---

## 3. A001：LSP 语义代码能力

### 3.1 InfCode 参考能力

本项主要参考本地 InfCode 源码：

- `infcode-dev/infcode-dev/packages/opencode/src/lsp/lsp.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/lsp/client.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/lsp/server.ts`

InfCode 使用 Language Server Protocol 获取比文本搜索和语法树更强的代码语义，包括定义、引用、类型悬浮信息、符号和诊断。其客户端会维护文档同步状态，并通过 `textDocument/didOpen`、`textDocument/didChange`、`workspace/didChangeWatchedFiles` 和 `textDocument/publishDiagnostics` 等协议消息与语言服务器交互。

### 3.2 NZ-Coder 原有不足

对齐前，NZ-Coder 主要依赖：

- `grep_search` 做文本匹配；
- `read_symbol` 和 `find_symbol_callers` 做 Python AST 级分析；
- `python_symbol_check` 做有限的 Python 结构检查。

这些能力无法可靠回答以下问题：

- 一个调用最终解析到哪个定义；
- 变量或表达式的推断类型；
- 跨模块、跨语言的真实引用关系；
- 编译器或类型检查器产生的即时诊断；
- 接口实现、调用层次等语义关系。

AST 工具仍然有价值，但它理解的是语法结构，不是完整的语言语义。

### 3.3 实现结果

NZ-Coder 新增了一个不依赖 Agent 框架和第三方 LSP 客户端库的轻量 LSP 运行层。

核心调用链：

```text
Agent 调用 lsp 工具
  -> tools/lsp.py
  -> lsp/manager.py 选择或复用客户端
  -> lsp/servers.py 识别语言、项目根目录和启动命令
  -> lsp/client.py 启动 language server
  -> stdio Content-Length JSON-RPC
  -> 返回定义、引用、类型、符号或诊断
```

已实现的主要能力：

- 按扩展名识别语言；
- 自动发现已安装的 language server；
- 支持通过环境变量覆盖不同语言的启动命令；
- 实现 LSP `initialize`、`initialized`、`shutdown` 和 `exit` 生命周期；
- 实现文档 `didOpen`、`didChange`、`didClose` 同步；
- 实现 watched-file 变化通知；
- 支持 definition、references、hover、document symbols、workspace symbols；
- 支持 implementations、call hierarchy 和 diagnostics；
- 按 workspace 与项目根目录缓存客户端；
- 启动失败隔离，不让某个 language server 破坏 Agent 主循环；
- 作为可选工具包延迟加载，未使用时不启动子进程。

### 3.4 写操作后的自动诊断

仅提供手动 `lsp` 工具仍然不够。InfCode 的重要体验是文件修改后能及时获得语义反馈，因此 NZ-Coder 又加入了写事务提交后的自动诊断。

执行时序：

```text
write_file / edit_file / apply_patch / write_files_batch
  -> 工具执行
  -> 所有写操作成功
  -> TransactionManager commit
  -> 把已提交文件同步给 LSP
  -> 优先请求 textDocument/diagnostic
  -> 不支持 pull 时等待 publishDiagnostics
  -> 收集 diagnostics
  -> 追加到最后一个写工具结果
```

这里最关键的设计决策是：**必须先提交事务，再发布 LSP 诊断**。

如果在事务仍然活动时通知 LSP，会产生两个问题：

1. 后续写工具失败并回滚时，语言服务器已经看见了临时文件状态；
2. Agent 可能收到针对最终并不存在内容的诊断。

因此失败路径保持为：

```text
任一写工具失败
  -> TransactionManager rollback
  -> 不调用写后 LSP
  -> 不发布临时诊断
```

`diagnostics()` 同时兼容两种服务器行为：先尝试
`textDocument/diagnostic` pull 请求；如果服务器返回“不支持”或超时，则在配置的等待窗口内接收
`textDocument/publishDiagnostics` push 通知，并返回该文档最后一次发布的诊断。

自动诊断采用 best-effort 策略。未安装服务器、协议不支持、超时或服务异常都不会让文件写入失败。

### 3.5 与 InfCode 不完全相同的设计

NZ-Coder 没有照搬 InfCode 的全部实现，主要差异如下：

| 设计点 | InfCode | NZ-Coder |
|---|---|---|
| LSP 客户端 | TypeScript 生态实现 | Python 标准库手写 stdio JSON-RPC |
| 服务安装 | 部分语言服务器可自动下载 | 不自动下载，只发现用户已安装的服务器 |
| 启动方式 | 产品运行时集成 | 可选工具包按需加载 |
| 写入安全 | 基于 InfCode 自身文件状态模型 | 明确接入 TransactionManager，提交后才诊断 |
| 依赖 | 可使用现有 TS 依赖 | 不新增外部框架或 LSP 客户端依赖 |

不自动下载 language server 是有意保留的边界：它避免 Agent 未经用户确认修改宿主环境，也符合 NZ-Coder 的轻依赖原则。

### 3.6 关键文件

- `nz_coder/lsp/client.py`：JSON-RPC、进程生命周期、文档同步和 LSP 请求。
- `nz_coder/lsp/servers.py`：语言映射、项目根目录、命令发现与环境变量覆盖。
- `nz_coder/lsp/manager.py`：客户端缓存、失败隔离和退出清理。
- `nz_coder/lsp/write_diagnostics.py`：事务提交后的文件同步与紧凑诊断格式。
- `nz_coder/tools/lsp.py`：面向模型的统一 LSP 工具。
- `nz_coder/tools/optional_loader.py`：可选工具包发现与加载。
- `nz_coder/runtime/loop.py`：写事务结束后触发诊断。
- `tests/test_lsp.py`：fake stdio server、路径安全、协议行为和错误降级测试。
- `tests/test_loop_fake.py`：写事务提交、批量路径收集和回滚不诊断测试。

### 3.7 配置项

```dotenv
NZ_LSP_ENABLED=1
NZ_LSP_INITIALIZE_TIMEOUT_SECONDS=20
NZ_LSP_REQUEST_TIMEOUT_SECONDS=10
NZ_LSP_DIAGNOSTIC_WAIT_SECONDS=2
NZ_LSP_WRITE_DIAGNOSTICS_ENABLED=1
NZ_LSP_WRITE_DIAGNOSTIC_MAX_FILES=8

# 可选：覆盖某种语言的启动命令
NZ_LSP_PYTHON_COMMAND=pyright-langserver --stdio
NZ_LSP_TYPESCRIPT_COMMAND=typescript-language-server --stdio
```

### 3.8 验证结果

完成本项时执行了以下验证：

- 新模块 `py_compile` 通过；
- `nz_coder/lsp/write_diagnostics.py` BasedPyright 严格检查为 `0 errors, 0 warnings`；
- LSP、Agent Loop、事务和钩子相关测试 `102 passed`；
- 完整测试套件 `345 passed`；
- 使用真实 `basedpyright-langserver 1.39.9` 验证成功；
- 真实返回了文件、行列、严重级别、诊断信息和规则编号。

主要复现命令：

```bash
basedpyright nz_coder/lsp/write_diagnostics.py
python3 -m pytest -q \
  tests/test_lsp.py tests/test_loop_fake.py tests/test_write_files_batch.py \
  tests/test_runtime_context.py tests/test_smoke.py tests/test_hooks.py
python3 -m pytest -q
```

验证环境快照：

- Python `3.13.12`；
- Linux `7.0.0-28-generic x86_64`；
- 验证针对当时工作区中的 NZ-Coder 文件快照；本项能力判断不依赖 Git 提交信息；
- language server：`basedpyright-langserver 1.39.9`。


本项没有运行 SWE-bench 官方评测。

### 3.9 学习重点

1. LSP 是构建在 JSON-RPC 之上的语言工具协议，不等于某个具体语言服务器。
2. `Content-Length` 消息分帧、请求 ID 和异步通知是客户端最基础的三部分。
3. 文档同步状态与磁盘文件状态必须区分。
4. 可选增强能力不能破坏核心写文件流程。
5. 在 Agent 系统中，工具正确性不仅是“函数返回正确”，还包括它与事务、回滚、消息顺序和模型上下文的关系。

### 3.10 设计边界与剩余差距

当前明确接受的设计边界：

- 不自动安装或管理 language server，避免 Agent 未经用户确认修改宿主环境。

后续待实现或待验证：

- 不同服务器对 diagnostics pull/push 的支持差异仍可继续兼容；
- Repo Map 的 LSP 语义区块已在 A004 接入排序；其他代码搜索候选尚未基于 LSP 结果重排；
- 尚未建立更完整的跨文件语义缓存；
- 缺少针对更多真实语言服务器的集成测试矩阵。

---

## 4. A002：Repo Map / 代码定义地图

### 4.1 InfCode 参考能力

本项参考：

- `infcode-dev/infcode-dev/archive/kilo-docs/pages/automate/tools/list-code-definition-names.md`

InfCode/Kilo 的 `list_code_definition_names` 用于快速建立代码库结构概念。它扫描源文件并输出类、函数、方法、接口等定义的位置与定义片段，使 Agent 在读取完整文件前先获得一张结构地图。

InfCode 当前还有远程 DeepMap 代码知识能力，但本项只对齐**本地代码定义地图**，不包含远程代码知识库、语义向量检索或 DeepMap 后端。

### 4.2 NZ-Coder 原有不足

NZ-Coder 已有 `read_symbol(mode="list")`，但它只解决单个 Python 文件的问题。Agent 面对陌生仓库时仍然需要：

1. 先用 `glob_search` 找文件；
2. 对多个文件分别调用 `read_symbol`；
3. 再根据结果决定读哪个文件。

这会增加工具轮次，也容易让模型在大型仓库中反复搜索。

### 4.3 实现结果

新增内置只读工具 `repo_map`，使用 Python 标准库 `ast` 递归建立跨文件结构索引。

核心调用链：

```text
Agent 调用 repo_map
  -> 验证 path 不逃逸 workspace
  -> os.walk 扫描并剪枝排除目录
  -> 读取 Python 文件轻量指纹
  -> 未变化文件复用内存缓存
  -> 变化文件重新 ast.parse
  -> 提取模块函数、类和直接类方法
  -> 按 query 过滤
  -> 输出文件、行号范围、符号类型和签名
```

工具参数：

| 参数 | 作用 |
|---|---|
| `path` | workspace 内的 Python 文件或目录，默认 `.` |
| `query` | 可选的空格分隔过滤词；所有词都必须出现在路径、符号类型、限定名和签名组成的联合文本中 |
| `max_files` | 最大扫描文件数 |
| `max_symbols` | AST 主层最大输出定义数；`semantic` 补充层另有 10 项硬上限 |
| `refresh` | 强制重新解析，不使用已有缓存 |

输出示例：

```text
# 调用参数包含 max_files=200；默认配置上限是 80
Python repository map
files_scanned: 143, files_matched: 3, symbols: 71, cache_hits: 0
query: AgentLoop

nz_coder/runtime/loop.py:
  153-1849 | class AgentLoop: class AgentLoop
  273-298 |   async method AgentLoop.run: async def run(...)
  993-1048 |   method AgentLoop._execute_tools: def _execute_tools(...)
```

### 4.4 增量缓存

缓存键由 workspace 和本次索引根路径组成。每个文件记录：

- 规范化路径；
- `st_mtime_ns`；
- 文件大小；
- 已解析的符号列表；
- 解析错误信息。

再次扫描时，如果修改时间和文件大小均未变化，就直接复用已有 `FileEntry`，避免重复执行 `ast.parse`。`refresh=true` 可强制重建。

这里的“轻量指纹”特指 `(st_mtime_ns, st_size)`，不是内容哈希。如果外部程序刻意保留纳秒修改时间并写入同样大小的内容，它可能漏判变化；遇到这种情况可使用 `refresh=true`。

这不是持久化索引：进程退出后缓存会消失。第一阶段选择内存缓存，是为了保持实现简单、避免索引文件污染仓库，并先验证 Repo Map 是否真正减少 Agent 搜索轮次。

### 4.5 安全与上下文控制

Repo Map 可能扫描大量文件，因此实现了以下限制：

- 所有输入路径必须通过 workspace 边界检查；
- 不跟随目录符号链接；
- 排除 `.git`、`.nz-coder`、`.venv`、`node_modules`、`build`、缓存目录等；
- 单文件大小超过配置上限时跳过；
- 限制最大扫描文件数；
- 限制最大输出符号数；
- 语法错误只跳过对应文件，不让整个工具失败；
- 工具被归类为 `READ_TOOLS` 和 `SAFE_TOOLS`；
- Reflection 子 Agent 也可以将它作为只读证据工具。

### 4.6 与 InfCode 不完全相同的设计

| 设计点 | InfCode `list_code_definition_names` | NZ-Coder `repo_map` |
|---|---|---|
| 解析器 | Tree-sitter，多语言 | Python 标准库 `ast` |
| 语言范围 | JS/TS、Python、Rust、Go、C/C++ 等 | A002 完成时仅 Python；A005 已扩展 |
| 目录范围 | 文档描述为指定目录顶层、最多 50 个文件 | 递归扫描，主动剪枝并配置上限 |
| 缓存 | 依赖其产品运行时与解析基础设施 | 进程内轻量指纹增量缓存 |
| 输出 | 定义源码片段 | 符号类型、限定名、行号范围和紧凑签名 |

NZ-Coder 没有引入 Tree-sitter，因为项目约束要求不新增外部依赖。Python AST 版本先覆盖 SWE-bench 和当前项目最常见的 Python 仓库。

### 4.7 关键文件

- `nz_coder/tools/repo_map.py`：索引、缓存、过滤、格式化和工具注册。
- `nz_coder/runtime/loop.py`：副作用 import，确保主 Agent 注册工具。
- `nz_coder/runtime/prompt.py`：提示陌生 Python 仓库先调用 Repo Map。
- `nz_coder/runtime/runtime_state.py`：把 Repo Map 观察为读取行为。
- `nz_coder/runtime/subagent.py`：加入 Reflection 只读工具集合。
- `nz_coder/tool_platform/permissioning/tool_groups.py`：声明为安全只读工具。
- `tests/test_repo_map.py`：结构、过滤、缓存、刷新、路径安全、排除目录和注册测试。

### 4.8 配置项

```dotenv
NZ_REPO_MAP_MAX_FILES=80
NZ_REPO_MAP_MAX_SYMBOLS=600
NZ_REPO_MAP_MAX_FILE_BYTES=1000000
```

### 4.9 验证结果

完成本项时执行了以下验证：

- `repo_map.py` BasedPyright：`0 errors, 0 warnings`；
- Repo Map、仓库智能、权限、运行状态和子 Agent 定向回归：`92 passed`；
- 完整测试套件：`351 passed`；
- 在 NZ-Coder 自身源码上真实扫描 `143` 个 Python 文件；
- 查询 `AgentLoop` 找到 `3` 个相关文件；
- 第二次相同调用缓存命中 `143` 个文件；
- 工具 handler 注册成功且工具规格仅出现一次；
- 无 `.orig`、`.rej` 补丁残留。

主要复现命令：

```bash
basedpyright nz_coder/tools/repo_map.py
python3 -m pytest -q \
  tests/test_repo_map.py tests/test_repo_intel.py tests/test_permissions.py \
  tests/test_runtime_state.py tests/test_subagent.py tests/test_smoke.py
python3 -m pytest -q
python3 -c 'import nz_coder.tools.repo_map; from nz_coder.tools import dispatch; print(dispatch("repo_map", {"path": "nz_coder", "query": "AgentLoop", "max_files": 200}))'
```

验证环境与 Git 基线同 A001。真实扫描使用了 `max_files=200`，因此扫描 `143` 个文件与默认配置上限 `80` 不冲突。


本项没有运行 SWE-bench 官方评测。

### 4.10 学习重点

1. Repo Map 的设计目标不是替代全文搜索，而是减少“我应该先看哪些文件”的探索成本；真实 Agent 日志是否证明这一收益仍待验证。
2. AST 索引适合提取结构，LSP 更适合解析语义，两者应互补。
3. 面向模型的工具必须主动限制输出，否则结构索引本身会成为上下文污染源。
4. 增量缓存的收益取决于 Agent 是否会在同一会话中反复探索同一仓库。
5. 工具实现完成后，还必须接入注册、权限、系统提示和运行状态，才能真正被 Agent 正确使用。

### 4.11 设计边界与剩余差距

当前第一阶段接受的设计边界：

- A002 完成时只支持 Python；A005 已加入多语言保守声明提取，但仍不是完整 Tree-sitter 语法树；
- 缓存不持久化，重新启动 NZ-Coder 后会重建；

后续待实现或待验证：

- 尚未把 Repo Map 结果作为首轮动态上下文自动注入；
- 还需要通过真实 Agent 运行日志验证它是否减少搜索工具调用次数。

相关性排序已在后续 A003 中完成第一阶段。

---

## 5. A003：分层文件与符号相关性排序

### 5.1 InfCode 参考能力

本项主要参考：

- `infcode-dev/infcode-dev/packages/opencode/src/infcode/context/ranking.ts`
- `infcode-dev/infcode-dev/packages/opencode/test/infcode/context/ranking.test.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/infcode/context/providers/FileContextProvider.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/infcode/context/providers/DirectoryContextProvider.ts`

InfCode 对文件和目录候选采用稳定的分层排序，而不是简单判断“匹配或不匹配”。核心顺序是：

```text
文件名精确匹配
  > 文件名前缀匹配
  > 文件名包含
  > 路径包含
  > 路径子序列模糊匹配
  > 文件名子序列模糊匹配
```

匹配质量优先于大小写敏感度；同一层级再根据 active、opened、文件名和路径稳定排序。它还会把 Windows 反斜杠查询归一为 `/`。

### 5.2 NZ-Coder 原有不足

A002 第一阶段中的 `repo_map(query=...)` 只有布尔过滤：

1. 把路径、符号类型、限定名和签名拼接；
2. 对查询按空格分词；
3. 所有词都出现则保留；
4. 结果仍主要按扫描顺序输出。

这意味着精确符号、前缀符号、普通包含和模糊路径没有明确优先级。例如，精确命中的 `Target` 可能排在路径字母序更靠前的 `MyTarget` 后面。

### 5.3 实现结果

新增独立的确定性排序模块 `repo_ranking.py`，并接入 `repo_map`。

NZ-Coder 根据 Repo Map 的使用场景，把 InfCode 的文件层级扩展为：

```text
0  符号名或限定名精确匹配
1  符号名或限定名前缀匹配
2  符号名、限定名或签名包含
3  文件名或 stem 精确匹配
4  文件名或 stem 前缀匹配
5  文件名或 stem 包含
6  完整路径包含
7  路径或限定名子序列模糊匹配
```

每个候选得到三元组：

```text
(最弱查询词的质量层级, 所有查询词层级之和, 大小写惩罚之和)
```

元组越小，相关性越高。多词查询保持 AND 语义：每个词都必须在符号、文件名、路径或模糊层中找到匹配；不同查询词可以分别命中不同字段。

核心调用链：

```text
repo_map(query)
  -> 为每个 FileEntry / SymbolEntry 调用 rank_repo_symbol
  -> 为每个查询词选择最强 MatchTier
  -> 任一查询词无匹配则丢弃该符号
  -> 文件以其最佳符号分数排序
  -> 文件内符号按分数、限定名和行号稳定排序
  -> 应用 max_symbols 输出限制
```

### 5.4 关键设计决策

#### 质量优先于大小写

与 InfCode 一致，大小写不敏感的精确匹配仍然优先于大小写敏感的部分匹配。只有查询中出现大写字符时，大小写才作为同一质量层内的次级排序依据；纯小写查询不会无意义地惩罚大写文件或符号。

#### 使用标准库子序列匹配

InfCode 使用 `fuzzysort`。NZ-Coder 不能新增外部依赖，因此使用确定性的标准库子序列判断。例如：

```text
ctxprov -> context/providers
```

这能覆盖跨目录缩写，但没有 `fuzzysort` 的复杂评分。当前实现只把模糊匹配放在最弱层，避免它压过明确的精确或包含匹配。

#### 不复制 active/opened 与 SERVER 层

InfCode 的排序服务于 IDE 文件选择菜单，因此包含 active tab、opened tab 和服务端已过滤候选的 SERVER fallback。A003 当时只处理本地 AST 主层，不是服务端结果的二次排序，所以没有复制这些产品状态；A004 后续为 LSP 补充层单独增加了 fallback。

### 5.5 关键文件

- `nz_coder/tools/repo_ranking.py`：匹配层级、大小写规则、路径归一、子序列匹配和多词聚合。
- `nz_coder/tools/repo_map.py`：对文件与符号应用排序结果。
- `tests/test_repo_ranking.py`：八层顺序、大小写、AND 查询、Windows 路径和真实 Repo Map 排序测试。
- `tests/test_repo_map.py`：原 Repo Map 行为回归。

### 5.6 验证结果

完成本项时执行了以下验证：

- `repo_ranking.py` 与 `repo_map.py` BasedPyright：`0 errors, 0 warnings`；
- 排序、Repo Map、仓库智能、权限、状态与子 Agent 定向回归：`98 passed`；
- 完整测试套件：`357 passed`；
- 真实多词查询 `_execute_tools AgentLoop` 扫描 `144` 个 Python 文件；
- 最终只返回 `nz_coder/runtime/loop.py`；
- 精确 `_execute_tools` 排在 `_execute_tools_async` 和模糊候选之前。

主要复现命令：

```bash
basedpyright nz_coder/tools/repo_ranking.py nz_coder/tools/repo_map.py
python3 -m pytest -q \
  tests/test_repo_ranking.py tests/test_repo_map.py tests/test_repo_intel.py \
  tests/test_permissions.py tests/test_runtime_state.py tests/test_subagent.py \
  tests/test_smoke.py
python3 -m pytest -q
python3 -c 'import nz_coder.tools.repo_map; from nz_coder.tools import dispatch; print(dispatch("repo_map", {"path": "nz_coder", "query": "_execute_tools AgentLoop", "max_files": 200, "max_symbols": 20}))'
```

验证环境与 Git 基线同 A001。本项没有运行 SWE-bench 官方评测。

### 5.7 学习重点

1. 搜索结果质量不只取决于“是否召回”，还取决于最相关结果能否稳定排在前面。
2. 排序应先比较匹配质量，再比较大小写或字母序；否则弱匹配可能因大小写偶然领先。
3. 多词查询用“最弱词层级 + 总层级”能避免一个强词掩盖另一个很弱的词。
4. 模糊匹配必须处于低优先级，否则会制造大量看似相关的噪声。
5. 对齐产品代码时，需要区分可复用算法和只属于原产品 UI 状态的逻辑。

### 5.8 设计边界与剩余差距

当前接受的设计边界：

- 使用简单子序列判断，不引入 `fuzzysort`；
- 不包含 IDE active/opened 状态；
- AST 主层不保留无匹配候选的 SERVER fallback；A004 的 LSP 补充层会保留服务器已过滤候选。

后续待实现或待验证：

- 尚未根据调用关系、测试文件权重或最近修改时间增加排序信号；
- 尚未把层级和匹配原因展示给模型；
- 尚未用真实 Agent trace 证明排序降低了搜索轮次或提升了修复成功率；
- 还没有把同一排序器复用到 `glob_search`、文件提及或其他候选列表。

---

## 6. A004：Repo Map 与 LSP workspace symbols 联动

### 6.1 InfCode 参考能力

本项主要参考本地 InfCode 源码：

- `infcode-dev/infcode-dev/packages/opencode/src/lsp/lsp.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/infcode/context/ranking.ts`

InfCode 的 `workspaceSymbol(query)` 会向当前 workspace 的所有活跃 LSP 客户端发送
`workspace/symbol` 请求，聚合返回结果，只保留以下八类结构性符号：

```text
class / function / method / interface
variable / constant / struct / enum
```

每个客户端最多返回 10 个结果；单个服务器失败时返回空列表，不中断整体请求。
这体现了两个重要原则：语义索引是多源聚合能力，同时必须以 best-effort 方式接入。

### 6.2 NZ-Coder 原有不足

A001 已实现独立 `lsp` 工具，A002/A003 已实现可查询、可排序的 Python AST Repo Map，
但两条链路彼此独立：

```text
repo_map -> Python ast
lsp(workspaceSymbol) -> language server
```

Agent 要获得语法地图和语义符号，必须主动调用两个工具，再自行关联路径、位置和符号。
Repo Map 也无法展示 AST 不覆盖的变量、常量、接口、枚举或其他语言服务器识别的结构。

### 6.3 实现结果

`repo_map` 新增可选参数：

```json
{"path": "nz_coder", "query": "AgentLoop", "semantic": true}
```

默认值为 `false`，因此原有纯 AST 行为、性能和工具接口保持兼容。启用后的调用链为：

```text
repo_map(path, query, semantic=true)
  -> 构建并排序 Python AST 地图
  -> 选择请求范围内的一个 Python 文件作为 LSP probe
  -> manager.get_client_for_file() 发现或复用 language server
  -> open_document() 同步 probe
  -> workspace/symbol(query)
  -> 过滤符号种类、非法位置、workspace 外路径和请求范围外路径
  -> 使用 A003 排序器排序，去重并限制数量
  -> 在 AST 地图后追加 “LSP workspace symbols” 语义区块
```

语义条目使用 1-based 位置，便于直接交给 `read_file` 或 `lsp` 工具：

```text
nz_coder/lsp/client.py:47:7 | class LSPClient
```

### 6.4 关键设计决策

#### AST 是稳定主层，LSP 是可选补充层

语言服务器可能没有安装、启动失败、超时或不支持 workspace symbols。若让 LSP 决定
Repo Map 是否成功，会把一个稳定的标准库能力变成环境相关能力。因此当前实现始终先生成
AST 地图；LSP 异常只产生 `semantic_notice`，不会返回 `Error:`，也不会丢失 AST 结果。

#### 严格限制路径和符号类型

语言服务器返回的是整个项目或 server root 的结果，不一定等于本次 `path` 参数的范围。
实现会同时验证：

1. URI 必须是本地 `file://`；
2. 解析后的文件必须位于 workspace 内；
3. 文件还必须位于本次请求的目录内，文件请求则必须精确匹配该文件；
4. 符号类型只采用 InfCode 的八类结构性符号；
5. 缺少 URI、range、非负位置或名称的条目直接跳过。

这既防止路径逃逸，也避免 property、field 等细粒度结果快速撑大上下文。

单次 LSP 补充层还硬限制为 10 项，与 InfCode 的每客户端上限一致。

#### 保留服务器已过滤结果

workspace symbol 结果已经经过 language server 按 query 过滤。A003 排序器负责把精确结果
排在前面；如果某个服务器候选无法被本地排序规则再次匹配，它仍以最弱 fallback 分数保留。
这个决策对齐了 InfCode 的 SERVER fallback 原则，避免客户端二次过滤误删服务器召回结果。

#### 非空查询的首次空响应只重试一次

真实 BasedPyright 验证发现：新客户端完成 `initialize` 后，第一次
`workspace/symbol` 可能立即返回空列表，但约 0.5 秒后索引即可用。实现无法区分“索引尚未
就绪”和“确实无匹配”，因此每次调用只要查询非空且首次响应是空列表，就等待 0.5 秒并
重试一次。空查询、异常、超时和非列表响应不会重试；成熟服务器上的真实无匹配查询会增加
至多 0.5 秒延迟，但不会持续轮询。

### 6.5 关键文件

- `nz_coder/lsp/workspace_symbols.py`：请求、过滤、路径约束、排序、去重、数量限制、首次空响应重试和文本格式化。
- `nz_coder/tools/repo_map.py`：新增 `semantic` 参数并组合 AST 主层与 LSP 补充层。
- `tests/test_lsp_repo_map.py`：覆盖类型过滤、范围约束、去重、排序、截断、异常降级、首次空结果重试和 Repo Map 集成。
- `README.md`：增加 `semantic=true` 使用示例和降级说明。
- `docs/architecture.md`：记录语义补充模块的架构职责。

### 6.6 验证结果

完成本项时执行了以下验证：

- `workspace_symbols.py` 与 `repo_map.py` BasedPyright：`0 errors, 0 warnings`；
- LSP/Repo Map/排序定向回归：`28 passed`；
- 完整测试套件：`363 passed`，只有 3 个既有第三方依赖告警；
- 真实 BasedPyright 冒烟扫描 `nz_coder/lsp` 的 6 个 Python 文件；
- AST 主层首先定位 `LSPClient` 及其方法；
- LSP 补充层返回 `nz_coder/lsp/client.py:47:7 | class LSPClient`；
- 第一次空结果的就绪时间实测约 0.5 秒，单次短重试后成功。

主要复现命令：

```bash
basedpyright nz_coder/lsp/workspace_symbols.py nz_coder/tools/repo_map.py
python3 -m pytest -q \
  tests/test_lsp_repo_map.py tests/test_repo_map.py \
  tests/test_lsp.py tests/test_repo_ranking.py
python3 -m pytest -q
python3 -c 'import nz_coder.tools.repo_map; from nz_coder.tools import dispatch; print(dispatch("repo_map", {"path": "nz_coder/lsp", "query": "LSPClient", "semantic": True, "max_files": 40, "max_symbols": 12}))'
```

本项没有运行 SWE-bench 官方评测，符合当前“先对齐 InfCode、暂不跑评测流程”的安排。

### 6.7 学习重点

1. 把两个能力放在同一个项目里不等于完成集成；真正的联动需要统一范围、排序、输出和失败语义。
2. LSP 的 `initialize` 成功只表示协议连接完成，不表示 workspace 索引已经就绪。
3. best-effort 不是简单吞异常，还要保留稳定主结果，并向调用者提供可理解的降级状态。
4. 服务端已经过滤过的候选不应被客户端用更严格规则无声删除。
5. 外部工具返回的 URI 即使来自可信 language server，也必须重新执行 workspace 和请求范围校验。

### 6.8 设计边界与剩余差距

当前接受的设计边界：

- `semantic` 默认关闭，避免普通 Repo Map 隐式启动子进程和增加延迟；
- 每次只通过一个 probe 获取一个匹配 server 的结果，尚未像 InfCode 一样聚合所有活跃客户端；
- A004 完成时 Repo Map 入口仍要求 Python；A005 已支持纯非 Python 结构地图，但混合项目的 `semantic` 补充仍只选择一个匹配 server；
- LSP 符号是独立补充区块，没有与 AST 条目按定义身份深度合并。

后续待实现或待验证：

- 多语言项目中聚合多个 LSP client，并按 server/root 去重；
- 缓存 workspace-symbol 结果，并根据写入通知精确失效；
- 对无 query 的大 workspace 设计更严格的召回策略；
- 将 definition/reference/call hierarchy 关系转化为 Repo Map 的调用边；
- 用真实 Agent trace 验证语义补充是否减少工具轮次。

---

## 7. A005：多语言结构地图

### 7.1 InfCode 参考能力

本项主要参考本地 InfCode 源码：

- `infcode-dev/infcode-dev/packages/opencode/src/lsp/server.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/lsp/lsp.ts`
- `infcode-dev/infcode-dev/packages/opencode/src/file/index.ts`

InfCode 没有为每种语言重复实现一个内置 AST Repo Map。它把文件扩展名映射到不同
language server，并通过相同的 LSP 接口获取 document/workspace symbols。其 server
配置覆盖 TypeScript/JavaScript、Python、Go、Rust、C/C++、Java、Kotlin、Ruby、PHP、
Lua、shell、Dart、YAML 等多种语言。

这说明可复用的核心不是“复制十套解析器”，而是：

1. 用统一语言映射发现源文件和可用 server；
2. 在相同符号模型中表示不同语言的结构；
3. server 缺失或失败时保持主流程可用；
4. 对返回规模和错误进行统一约束。

### 7.2 NZ-Coder 原有不足

A002 的 Repo Map 只扫描 `.py`，A004 虽然能用 LSP workspace symbols 补充语义，但仍
需要先找到 Python probe。因此纯 TypeScript、Go、Rust 或其他语言目录会返回：

```text
No Python files found ...
```

这带来三个实际问题：

- Agent 进入非 Python 仓库时无法先看结构地图；
- 即使已安装对应 language server，也没有源文件入口可用于启动它；
- 混合语言仓库只能索引 Python 部分，查询和排序会忽略其他语言的候选。

### 7.3 实现结果

Repo Map 现在使用同一条多语言索引管线：

```text
repo_map(path)
  -> lsp/servers.language_for_path() 统一识别语言
  -> 扫描受支持源文件并执行 workspace/排除目录/大小限制
  -> Python / .pyi 使用 ast.parse
  -> 其他受支持语言使用保守声明提取
  -> 统一转换为 FileEntry / SymbolEntry
  -> 复用 A002 增量缓存
  -> 复用 A003 分层查询排序
  -> semantic=true 时用最相关文件作为 A004 LSP probe
  -> 输出语言列表、文件、行号、符号类型、名称和紧凑签名
```

当前主层覆盖：

| 语言族 | 扩展名示例 | 主层策略 |
|---|---|---|
| Python | `.py`、`.pyi` | 标准库 AST，包含类、函数和直接方法 |
| TypeScript/JavaScript | `.ts`、`.tsx`、`.js`、`.jsx`、`.mjs` 等 | 类型、接口、类、函数、箭头函数、变量/常量行形态（无法确认作用域） |
| Go | `.go` | type、struct、interface、function、receiver method |
| Rust | `.rs` | struct、enum、trait、type、mod、function |
| Java | `.java` | class、interface、enum、record |
| Kotlin | `.kt`、`.kts` | class、interface、object、function |
| C/C++ | `.c`、`.cpp`、`.h` 等 | class、struct、enum、单行函数定义 |
| Ruby | `.rb`、`.rake` 等 | class、module、function |
| PHP | `.php` | class、interface、trait、enum、function |
| Lua | `.lua` | local/global function |
| Shell | `.sh`、`.bash`、`.zsh`、`.ksh` | 两种标准 function 声明 |

纯 Python 结果仍以 `Python repository map` 开头，主体格式保持兼容，但会新增
`languages: python` 元数据行，因此不保证严格文本相等。只要包含其他语言，标题改为
`Source repository map`，并增加例如：

```text
languages: go, rust, typescript
```

### 7.4 关键设计决策

#### 不引入 Tree-sitter

Tree-sitter 能提供更完整的多语言语法树，但会新增原生依赖、语言 grammar 包、平台构建和
版本兼容成本，不符合当前“除 openai/tiktoken 外不新增依赖”的约束。本阶段使用 Python
标准库 `re` 实现高置信度声明提取，把完整语义交给可选 LSP 层。

#### 保守召回优先于伪精确解析

规则全部锚定在行首声明形态，只识别明确的 class/function/type 等结构。它不会尝试解析
表达式、作用域、宏展开或完整类型系统。常见单行注释和简单 C 风格 `/* ... */` 块注释
会跳过；Ruby `=begin`、Lua `--[[` 等语言专用块注释尚未处理。C/C++ 控制语句不会被
当成函数，shell 只接受 `function name {` 或 `name() {`。

这种设计会漏掉多行声明或复杂语法，但比大量误报更适合 Agent 的首次仓库浏览。

#### 复用 LSP 的语言映射

`language_for_path()` 从 `lsp/servers.py` 暴露只读语言识别，Repo Map 不再维护第二份扩展名
表。以后增加扩展名或调整语言族时，server discovery 与结构地图不会产生明显漂移。

#### semantic probe 跟随查询相关性

A004 固定选择扫描到的第一个 Python 文件。A005 改为优先选择排序后最相关文件，因此在
混合项目中查询 TypeScript `TargetService` 时，会用对应 `.ts` 文件启动 TypeScript server，
而不是因字母序先选中无关的 Go/Python server。

如果本地声明提取没有任何查询匹配，当前实现会退回扫描到的第一个受支持文件；此时混合项目
可能选择错误语言的 server。相关文件选择目前由 fake collector 集成测试覆盖，尚未用真实
TypeScript language server 验证。

### 7.5 关键文件

- `nz_coder/tools/repo_languages.py`：非 Python 语言的高置信度声明模式、注释跳过和统一符号输出。
- `nz_coder/tools/repo_map.py`：多语言扫描、Python AST/声明提取分流、语言统计和相关 probe 选择。
- `nz_coder/lsp/servers.py`：新增共享的 `language_for_path()` 只读接口。
- `tests/test_repo_languages.py`：十个语言族、混合目录、纯非 Python 文件、排序和 semantic probe 测试。
- `tests/test_repo_map.py`：Python 行为和 unsupported source 错误回归。
- `README.md`：多语言 Repo Map 使用方式。
- `docs/architecture.md`：结构提取模块职责。

### 7.6 验证结果

完成本项时执行了以下验证：

- `repo_languages.py` 与 `repo_map.py` BasedPyright：`0 errors, 0 warnings`；
- 多语言、Repo Map、排序和 LSP 定向回归：`41 passed`；
- 完整测试套件：`376 passed`，只有 3 个既有第三方依赖告警；
- 真实扫描本地 InfCode `packages/opencode/src/lsp` 的 6 个 TypeScript 文件；
- 精确查询 `workspaceSymbol` 只匹配 `lsp.ts`；
- 返回 `lsp.ts:463 constant workspaceSymbol` 及其 `Effect.fn` 声明签名。
- 真实冒烟验证的是本地 TypeScript 声明提取，不代表真实 TypeScript LSP 集成已经通过。

主要复现命令：

```bash
basedpyright nz_coder/tools/repo_languages.py nz_coder/tools/repo_map.py
python3 -m pytest -q \
  tests/test_repo_languages.py tests/test_repo_map.py \
  tests/test_repo_ranking.py tests/test_lsp_repo_map.py tests/test_lsp.py
python3 -m pytest -q
python3 -c 'import nz_coder.tools.repo_map; from nz_coder.tools import dispatch; print(dispatch("repo_map", {"path": "infcode-dev/infcode-dev/packages/opencode/src/lsp", "query": "workspaceSymbol", "max_files": 100, "max_symbols": 20}))'
```

本项没有运行 SWE-bench 官方评测，符合当前“先对齐 InfCode、暂不跑评测流程”的安排。

### 7.7 学习重点

1. 多语言能力首先是统一发现、统一符号模型和统一失败语义，不只是增加正则数量。
2. 在不能增加 parser 依赖时，保守的声明召回加可选 LSP 比假装完整解析更可靠。
3. 扩展名映射属于共享基础设施，Repo Map 和 LSP 各维护一份会快速漂移。
4. 有本地声明召回时，混合项目的 semantic probe 应由查询相关性决定，而不是文件扫描顺序。
5. 真实产品源码冒烟能暴露样例没有覆盖的声明形态，例如 `const name = Effect.fn(...)`。

### 7.8 设计边界与剩余差距

当前接受的设计边界：

- 非 Python 主层是声明提取，不是完整语法树；
- 除 Python 外，`end_line` 当前等于声明起始行；
- 多行函数签名、装饰器/注解组合、嵌套类型、宏和生成代码可能漏召回；
- 方法识别目前只对 Go receiver 等明确形态可靠；
- YAML、Dart 等虽有 LSP server 映射，但尚无主层结构提取规则。

后续待实现或待验证：

- 本地查询无匹配时 semantic probe 会退回首个文件，可能选错语言 server；
- 聚合混合项目中的多个 LSP client，而不是只选择一个 probe/server；
- 根据真实误报/漏报数据逐步扩展语言规则；
- 评估把 Tree-sitter 做成完全可选的增强包，而不是核心依赖；
- 为非 Python 语言增加更准确的作用域和限定名；
- 用真实 Agent trace 验证多语言地图是否减少 grep/read_file 轮次。

---

## 8. A006：三级记忆与会话压缩复核

A006 没有重新发明一套记忆系统，而是先区分 Working、Session 和 Long-term 三层，再只修复
源码对照后能够复现的缺口。

### 8.1 InfCode 参考能力

本次主要参考本地 InfCode 以下实现：

- `packages/opencode/src/session/todo.ts`：todo 以 `sessionID` 为键写入数据库，进程重启后仍可恢复；
- `packages/opencode/src/session/compaction.ts`：固定结构摘要、上一摘要锚定、按完整 user turn
  选择近期 tail，并按模型窗口限制保留预算；
- `packages/opencode/src/infcode/knowledge/storage.ts` 与
  `context/providers/KnowledgeContextProvider.ts`：显式文档知识库存储、选择和按需检索入口。

InfCode 的知识库不是自动从每次聊天提取“个人记忆”的同义实现，因此本项没有为了表面一致
而删除或重写 NZ-Coder 已有的长期记忆管线。

### 8.2 NZ-Coder 原有状态与真实不足

复核结果不是“三层都没有”，而是成熟度不均：

| 层级 | A006 前已有能力 | 确认的主要缺口 |
|---|---|---|
| Working Memory | scratchpad 计划/失败、todo、RuntimeState | scratchpad/todo 只在进程内，且每个 `run()` 开始清空 scratchpad |
| Session Memory | 消息自动保存、`/resume`、超大输入/工具输出落盘、自动压缩 | 压缩替换全部历史，没有近期原始回合和上一摘要锚点 |
| Long-term Memory | 自动提取、类型化存储、相关性召回、去重、dream 清理、可选 Dodo | 与 InfCode 显式文档知识库目标不同，但本次没有发现需要推倒重做的 P0 缺口 |

可从源码直接复现的问题：

- `Scratchpad` 和 todo 的字典只按 session id 存在当前 Python 进程中；相同 session id 在不同
  workspace 中还可能发生缓存碰撞；
- `AgentLoop._init_run()` 每个用户回合调用 `self._sp.clear()`，所以所谓 session-scoped
  Working Memory 实际退化成 run-scoped；
- `auto_compact()` 把全部历史替换成一条 user 摘要，近期工具结果和原始对话一起丢失，第二次
  压缩也无法显式区分旧摘要与新增历史。

对 Agent 的实际影响：

- `/resume` 后模型能看到消息历史，却恢复不了活跃计划、失败摘要和 todo；
- 压缩后更容易丢失最近错误输出、精确命令和正在执行的下一步；
- 多次压缩会反复总结摘要文本，细节逐步漂移。

### 8.3 实现结果

核心调用链分为两条：

```text
workspace + session id
  -> scratchpad.json / todo.json 原子写入 session runtime 目录
  -> 新进程或 /resume 时惰性加载
  -> 每轮动态上下文继续注入 scratchpad，todo reminder 继续工作

context 超预算或手动 /compact
  -> 忽略旧 summary 标记，以普通 user 输入作为 turn 起点选择最多两个近期完整回合
  -> 其余普通历史构成 head，最新旧摘要只作为 <previous-summary> 锚点
  -> head 使用固定 Markdown 模板生成新摘要
  -> 单个新 <session-summary> + 预算内未改写 recent tail 重新组成消息列表
```

#### 持久化 Working Memory

`state/sessions.py` 新增 `session_scratchpad_path()` 和 `session_todo_path()`，路径始终由经过
清洗的 session id 和当前 workspace 派生，不接受模型提供的任意路径。

Scratchpad 现在：

- 以 `(workspace, session_id)` 作为进程内缓存键，避免跨仓库碰撞；
- 首次读取时从 JSON 恢复，update/replace/clear 通过唯一临时文件、flush/fsync 和原子 rename 提交；
- 不再在每个 `AgentLoop.run()` 开始时自动清空，只有 `/clear` 或新的 session id 会隔离状态。

Todo 现在：

- 使用同样的 workspace/session 隔离、惰性加载和原子写入；
- 向后兼容原有 `content/status` 参数，并增加可选 `priority` 与 `cancelled` 状态；
- `/clear` 同时清空 scratchpad 和 todo，完成/取消的 todo 不再触发开放任务 reminder。

#### Anchored compaction 与近期回合保留

`auto_compact()` 现在：

- 带稳定 `<session-summary>` 标记的 user 消息不算普通 turn 起点；
- 从最新普通 user turn 向前选择最多两个完整 turn，避免从 assistant/tool 序列中间截断；
- tail 总计受 32,000 字符预算约束，过大时退回更短 tail 或只保留摘要；
- 除 tail 之外的普通历史构成 head；
- 如果 head 中存在旧摘要，只取最新一份作为 `<previous-summary>` 锚点；所有旧 summary 包装消息
  都从普通 head 输入中排除，不会重复概括；
- head 使用包含 Goal、Constraints、Progress、Decisions、Next Steps、Critical Context 和
  Relevant Files 的固定模板，最终只生成一个新的 `<session-summary>`；
- 被预算选中的 recent tail 原样追加，因此其中的 tool-call 配对和错误文本不会被改写；超出预算
  的旧回合只由摘要承载，不保证保留原文。

#### 长期记忆复核结论

本项确认 `state/memory.py` 已经具备：

- 规则或可选 LLM session learning 提取，并过滤内部 reminder/hook 噪音；
- 类型化 memory、去重/合并、词法相关性召回、可选 LLM rerank 和 prompt 字符预算；
- 阈值式 dream 清理，以及完全可选的 Dodo 混合检索后端。

这些能力与 InfCode 的显式文档知识库并非一一对应。本阶段保持现有接口，只记录“自动提取默认
仅在 auto permission mode 触发”等边界，不做无证据的大规模重构。

### 8.4 关键设计决策

#### 使用小型 JSON checkpoint，不引入数据库

InfCode 已有 SQLite/Drizzle 基础设施，NZ-Coder 核心约束则是不新增框架或依赖。每个 session
只保存最多 20 条 scratchpad/todo，小 JSON 加临时文件原子替换足够，也便于学习和排查。

#### 缓存键必须包含 workspace

只用 session id 做全局字典键，在两个仓库复用相同 id 时会串状态。磁盘路径原本按 workspace
派生，进程内键也必须保持同样隔离边界。

#### 保留完整 turn，而不是最后 N 条 message

直接保留最后 N 条消息可能从 tool result 开始，制造非法或难懂的对话序列。以 user message
作为 turn 起点，再整体保留 assistant/tool 尾部，能够保持协议配对和人类可读性。

#### 不把 InfCode knowledge base 强行等同于个人长期记忆

InfCode 参考实现是用户显式选择的文档集合；NZ-Coder 的 memory 是自动学习的规则和偏好。两者
可以未来并存，但不能因为名字都叫“知识/记忆”就替换彼此。

### 8.5 关键文件

- `nz_coder/state/sessions.py`：session 工作状态文件的派生路径。
- `nz_coder/tools/scratchpad.py`：workspace/session 双重隔离、惰性恢复和原子持久化。
- `nz_coder/tools/todo.py`：持久化 checklist、priority/cancelled 兼容与 clear。
- `nz_coder/state/context.py`：固定摘要模板、head/tail 选择、上一摘要锚定和 recent tail 拼接。
- `nz_coder/runtime/loop.py`：停止每回合清空 scratchpad，`/clear` 同时清理两个 Working Store。
- `tests/test_scratchpad.py`：跨实例/新 Python 进程恢复、持久化 clear、todo 状态和优先级测试。
- `tests/test_context_budget.py`：完整近期 turn 保留与第二次 anchored compaction 测试。
- `tests/test_loop_fake.py`、`tests/test_subagent.py`：运行时保留和目录创建兼容回归。

### 8.6 验证结果

完成本项时执行了以下验证：

- 相关 Python 文件 `py_compile` 通过；
- A006 核心实现与测试 Ruff：`All checks passed`；
- Working Memory、context、CLI、runtime、loop 和 subagent 定向回归：`74 passed`；
- 完整测试套件：`382 passed`；
- 只有 3 个既有 websockets/uvicorn/multiprocessing 第三方告警；
- 独立 Python 子进程成功从相同 workspace/session 的 JSON checkpoint 恢复 scratchpad；
- CLI `/resume` 回归确认继续使用保存的 session id 和消息；两项分别验证，未宣称完整交互式端到端；
- 在测试输入未超过 32,000 字符 tail 预算时，fake client 双次压缩验证上一摘要进入
  `<previous-summary>`，最近两个完整回合保持原文；
- 本项没有运行 SWE-bench 官方评测。

主要复现命令：

```bash
python3 -m py_compile nz_coder/state/sessions.py nz_coder/tools/scratchpad.py nz_coder/tools/todo.py nz_coder/state/context.py
ruff check nz_coder/state/sessions.py nz_coder/tools/scratchpad.py nz_coder/tools/todo.py nz_coder/state/context.py tests/test_scratchpad.py tests/test_context_budget.py
python3 -m pytest -q tests/test_scratchpad.py tests/test_context_budget.py tests/test_cli_commands.py \
  tests/test_runtime_context.py tests/test_loop_fake.py tests/test_subagent.py
python3 -m pytest -q
```

### 8.7 学习重点

1. “session-scoped” 必须同时验证作用域、进程重启和 resume，只有字典分组不等于持久化。
2. Working Memory 与聊天历史互补：前者保存活跃计划/失败，后者保存完整交互证据。
3. 会话压缩的关键不是摘要文笔，而是稳定 schema、增量锚点和可验证的 tail 边界。
4. 最近原始回合比再次总结更可靠，尤其包含精确错误、命令和 tool-call 配对时。
5. 对齐参考项目时要先比较目标语义；显式文档知识库和自动个人记忆不是同一产品能力。

### 8.8 设计边界与剩余差距

当前接受的设计边界：

- scratchpad 与 todo 各最多 20 条，适合轻量工作状态，不是事件数据库；
- 同一 session 的旧计划会一直保留到新计划替换或用户 `/clear`，可能出现短暂陈旧信息；
- JSON 采用原子替换保证文件完整，但多个独立进程同时写同一 session 时是 last-writer-wins；
- compaction tail 目前以字符数近似预算，而不是调用模型 tokenizer 精确计算；
- recent tail 最多两个 user turn，超大回合会退回更短 tail 或只保留摘要。

后续待实现或待验证：

- 自动 session learning 默认仅在 `auto` permission mode 触发，其他模式仍依赖显式 `save_memory`；
- 尚无 InfCode 风格的用户文档知识库导入、构建状态和 `knowledge_search` 工具；
- 尚未为多个进程同时更新同一 Working Store 增加文件锁或版本冲突检测；
- LLM 摘要仍可能遗漏事实，需要继续依赖 transcript、scratchpad 和原始 recent tail 兜底；
- 需要真实 Agent trace 验证 resume/多次压缩后是否减少重复搜索和丢失上下文。

## 9. A007：并行工具调用与副作用屏障

### 9.1 InfCode 参考能力

本项复核基于当前本地 InfCode 源码快照，重点参考：

- `packages/opencode/src/session/prompt.ts`：每个工具注册为异步 `execute()` 回调，调用前后分别触发插件 hook；
- `packages/opencode/src/session/llm.ts`：把完整 tools map 交给 AI SDK `streamText()`；
- `packages/opencode/src/session/processor.ts`：按 call id 独立维护 pending/running/result/error 状态。

从这几处源码能确认：InfCode 自身没有在 session loop 中加入“只要一批中存在写工具，就把整批
全部串行”的总开关。每个工具以独立 async `execute()` 回调注册；是否重叠执行以及具体时序
由 AI SDK 和 provider 事件流负责。本文只记录源码可证明的边界，不把未直接审计的 SDK 内部实现描述成
`Promise.all`。

### 9.2 NZ-Coder 原有状态与真实不足

A007 前并不是完全没有并行能力：

- `_execute_concurrent()` 已能用线程池执行整批非写工具；
- `_execute_concurrent_async()` 已使用 `asyncio.gather()`；
- `MAX_PARALLEL_TASKS` 已限制并发量；
- explore/plan/reflection 子任务已有离线加速基准，并保持模型返回顺序。

但原调度有两个相反的问题：

1. **并行范围过宽**：判断逻辑基本等价于“工具名不在 `WRITE_TOOLS` 就可并行”。因此
   `bash`、`todo`、`update_scratchpad`、memory 写入、动态工具加载和未知插件工具都可能进入
   并发段。这些工具虽然不一定写代码文件，却会修改进程、会话、注册表或磁盘状态。
2. **混合批次过度串行**：一个批次只要包含任意文件写工具，前后的纯读取也全部串行，无法利用
   已经存在的并行执行器。

此外，`register()` 只有 schema 和 handler，没有执行效果元数据。动态扩展工具即使会写文件，
也无法自动进入主循环事务或只读子代理隔离。

### 9.3 实现结果

工具注册现在支持内部 `execution` 元数据：

```python
register(
    name="read_file",
    description="...",
    parameters={...},
    handler=read_file,
    execution="read",
)
```

支持三种效果：

| execution | 调度语义 | 典型工具 |
|---|---|---|
| `read` | 可与相邻显式只读调用并行 | read_file、grep/glob、Repo Map、符号读取 |
| `serial` | 顺序屏障；默认值 | todo、Scratchpad 写入、compact、动态加载、未知工具 |
| `write` | 顺序屏障，并进入写事务/只读子代理隔离 | write/edit/apply_patch、结构化编辑、批量写入 |

核心调用链：

```text
模型返回有序 tool calls
  -> 截取 MAX_TOOL_CALLS_PER_RESPONSE 前缀
  -> 对每个调用查询 execution effect
     -> task: 只有 explore/plan/reflection 可并行
     -> bash: 只有保守只读命令白名单可并行
     -> 普通工具: 只有 execution="read" 可并行
  -> 连续 read 调用组成 parallel segment
  -> serial/write 调用形成顺序屏障
  -> 每个 parallel segment 受 MAX_PARALLEL_TASKS 限制
  -> 按模型原始 index 重新排列 tool results
  -> 再执行 trace、hook、消息追加和事务收尾
```

例如模型返回：

```text
read_file(A), grep_search(B), edit_file(A), read_file(A), diff_status()
```

调度时序为：

```text
[read_file(A) || grep_search(B)]
  -> edit_file(A)
  -> [read_file(A) || diff_status()]
```

这样后半段读取一定发生在编辑之后，不会跨越写屏障看到旧状态；同时前后两个只读段仍能获得
并行收益。

`is_write_tool()` 将静态内置写工具集合与动态 `execution="write"` 合并，统一用于：

- `ToolExecutionResult.is_write`；
- AgentLoop 是否开启 TransactionManager 生命周期；
- default/plan/acceptEdits 权限模式的 ask/deny/allow 判定；
- pre/post hook 的写工具标记；
- 只读子代理的工具规格过滤；
- general-purpose 子代理的写入、验证和回滚状态。

`execution="write"` 能让动态工具进入权限、写批次和隔离边界，但元数据本身不会拦截任意 `Path.write_text()`。
动态 handler 若要获得真实文件回滚，仍必须使用已注入 TransactionManager/ChangeTracker 的文件 API。

### 9.4 关键设计决策

#### 未声明效果的工具默认串行

并发安全不能由“没有被发现会写”推导。第三方或后续工具可能修改缓存、session、数据库、子进程
或注册表，因此 `serial` 是兼容且保守的默认值。只有作者明确声明 `read` 才允许并行。

#### 使用顺序屏障，不跨越副作用调用重排

把批次中所有 read 抽出来一起执行会改变模型指定的因果顺序。例如“写后再读”可能读到写前
状态。连续分段允许局部并行，但任何 serial/write 都会先等待前段完成，并阻止后段提前开始。

#### 发布顺序与完成顺序分离

线程可能乱序完成，但发回模型的 tool result 必须与原始 tool-call index 对齐。分段执行器使用
索引适配器把局部 worker index 映射回模型原始 index，最终始终按原顺序追加消息、运行 after
hook 和写 trace。

#### 读取失败不会隐式跳过后续屏障

并行 worker 抛出的异常会被转换成对应 call id 的 `Error: tool execution raised: ...` 结果；同段其他
读取仍会完成。当前批次随后继续执行后面的 serial/write 屏障和读取段，而不是 fail-fast。如果本批
包含写工具，任意 dispatch failure 会让批次结束时回滚已被 TransactionManager 跟踪的文件写入；
所有错误结果仍按原始 index 发布。独立 timeout、取消传播和 sibling cancellation 仍未实现。

#### pre-tool hook 存在时保持整批串行

pre-tool hook 可以读取或修改共享 messages，也可能拒绝某次调用。A007 保留原有保护：只要配置了
pre-tool hook，本批工具不并行，避免 hook 在多个线程中观察到不一致的会话状态。

#### 不机械复制 InfCode 的完全异步边界

InfCode 把异步 execute 交给 AI SDK；NZ-Coder 还有跨多个写工具的 TransactionManager 和
ChangeTracker。为了保证回滚、写后 LSP 和只读子代理隔离，本阶段有意让所有写与状态副作用工具
保持屏障，只对经过证明的读取并行。

### 9.5 关键文件

- `nz_coder/tools/__init__.py`：注册 read/serial/write 执行效果，未知工具默认 serial。
- `nz_coder/runtime/tool_executor.py`：统一动态/内置写工具判定与执行结果分类。
- `nz_coder/tool_platform/permissioning/checker.py`：动态 write 在 default/plan/acceptEdits 中沿用写权限。
- `nz_coder/runtime/loop.py`：同步/异步连续只读段调度、Bash 和 task 动态分类、原始顺序恢复。
- `nz_coder/runtime/subagent.py`：动态写工具不会进入 explore/plan/reflection 工具集合。
- `nz_coder/tools/files.py`、`search.py`、`python_ast.py`、`repo_map.py`、`repo_intel.py`：核心工具效果标注；`diff_status` 明确为 read。
- `tests/test_runtime_context.py`：屏障时序、同步/异步一致性、默认串行和 Bash 分类测试。
- `tests/test_subagent.py`：动态 write 工具的只读子代理隔离测试。
- `tests/test_permissions.py`：动态 write 的 default/plan/acceptEdits 权限测试。
- `nz_coder/evaluation/parallel_benchmark.py`：无模型、无网络的离线调度加速基准。

### 9.6 验证结果

完成本项时：

- A007 调度、权限、主循环事务、子代理隔离、注册兼容、scaffold 与离线基准定向回归：`111 passed`；
- 完整测试套件：`387 passed`；
- 只有 3 个既有 websockets/uvicorn/multiprocessing 第三方告警；
- 测试平台：Linux 7.0.0-28-generic x86_64，Python 3.13.12；
- 相关模块 `py_compile` 通过；
- 聚焦 A007 新增代码的 Ruff 检查通过；历史文件原有 F401/E402/F811 未纳入本项清理；
- 6 个 50 ms 离线任务、并发上限 3：串行约 0.301 s，并行约 0.102 s，约 `2.95x`，
  peak concurrency 为 3，结果顺序保持；
- 没有运行 SWE-bench 官方评测，也没有发送模型请求。

关键断言对应关系：

- `test_mixed_tool_batch_parallelizes_read_segments_around_serial_barrier`：两侧 read 段、屏障时序、同步/异步一致；
- `test_tool_concurrency_requires_explicit_read_effect`：只读 Bash、状态工具和未知工具分类；
- `test_tool_execution_metadata_is_idempotent_and_defaults_to_serial`：注册默认值、重复注册和非法 mode；
- `test_dynamic_write_tool_is_hidden_from_read_only_subagents`：动态 write 子代理隔离；
- `test_dynamic_write_effect_is_enforced_by_permission_modes`：动态 write 权限模式。

主要复现命令：

```bash
python3 -m pytest -q tests/test_permissions.py tests/test_runtime_context.py \
  tests/test_parallel_benchmark.py tests/test_subagent.py tests/test_loop_fake.py \
  tests/test_smoke.py tests/test_scaffold_project.py
python3 -m nz_coder.evaluation.parallel_benchmark \
  --tasks 6 --delay 0.05 --parallel-limit 3 --json
python3 -m pytest -q
```

### 9.7 学习重点

1. “非写文件工具”不等于“只读工具”；会话状态、memory、registry 和 shell 都是副作用。
2. 安全并行应采用显式 opt-in，未知扩展默认串行。
3. 混合批次不能简单全并行或全串行；顺序屏障能同时保留因果关系和局部加速。
4. 并发完成顺序可以变化，但 provider 协议中的 tool-call/result 对应关系不能变化。
5. 调度元数据必须贯穿事务、hook 和子代理隔离，不能只影响线程池分支。

### 9.8 设计边界与剩余差距

当前接受的边界：

- async 并发限制采用分批 gather；一个慢调用会阻塞下一批，而不是滚动 semaphore 调度；
- 有副作用的同步工具仍会在 async loop 的串行屏障处阻塞当前协程；
- LSP 虽是查询工具，但文档同步和客户端缓存尚未证明完整线程安全，因此仍默认 serial；
- 未标注的旧工具和第三方工具默认 serial，安全性优先于最大吞吐；
- `execution` 是工具作者声明的信任边界：误把副作用工具标成 read 可能制造竞态，注册层无法自动证明纯度；
- 漏标 write 的动态工具虽然因默认 serial 不会并发，却不会自动获得写权限、事务生命周期或只读子代理隔离；
- 正确标注的动态 write 仍无法替代 handler 对事务安全文件 API 的接入；
- trace 和 after hook 按发布顺序执行，不记录每个 worker 的真实完成先后时间；
- 没有实现单个并行工具的独立 timeout、取消传播或 fail-fast sibling cancellation。

后续可继续对齐：

- 用 semaphore/任务队列实现滚动并发，同时保留顺序屏障；
- 为 execution 元数据增加注册时审计或测试辅助，避免纯读取工具漏标；
- 增加 per-call start/end/duration trace，区分执行完成顺序和结果发布顺序；
- 在证明 LSP/document cache 线程安全后，再决定是否允许并行语义查询；
- 用真实 Agent trace 统计 read batch 命中率、平均并发度和实际端到端节省时间。

## 10. A008：保守恢复与重复工具调用防线

A008 最重要的产出不是新增一个名为 frozen symbol 的模块，而是通过源码审计纠正了最初的
功能假设：InfCode 有最小改动提示和重复调用防线，但在当前源码快照中没有“冻结已确认代码符号”的
通用实现。NZ-Coder 真正缺少的是会在工具实际执行前阻止无进展重复调用的运行时 guard。

### 10.1 InfCode 参考能力

本项复核基于当前本地 InfCode 源码快照，重点参考：

- `packages/opencode/src/session/processor.ts`：`DOOM_LOOP_THRESHOLD = 3`；最近三个 tool part
  的工具名和输入完全相同时，请求 `doom_loop` 权限；
- `packages/opencode/src/session/retry.ts`：只对可重试 API/网络错误做退避、离线重连和连续无进展
  上限控制；一旦流式输出有进展就重置连续失败预算；
- `packages/opencode/src/session/llm.ts`：`experimental_repairToolCall` 只修复可识别的工具名大小写，
  其余失败调用转成 `invalid` 工具结果，并不自动重写代码修复方案；
- `packages/opencode/src/tool/edit.ts`、`write.ts` 和 `apply_patch.ts`：在真实磁盘写失败时回滚
  DiffList/checkpoint 记录，并暴露检查点不完整状态；
- `packages/opencode/src/session/prompt/gpt.txt` 与 `kimi.txt`：明确要求优先选择最小正确改动、
  先读代码和失败证据、修复后运行测试。

必须区分三类名字都像“retry”的行为：

| 机制 | 触发条件 | 作用 | 不负责什么 |
|---|---|---|---|
| API/network retry | 5xx、限速、断网等瞬态错误 | 退避后重发同一模型请求 | 不改变 patch 策略 |
| tool-call repair | 工具名大小写或不可解析调用 | 修正名字或转成 invalid 结果 | 不修复业务代码 |
| business repair iteration | 测试/工具结果证明代码仍不正确 | 根据证据改代码并执行最小相关验证 | 不是同一 API 请求的自动重发 |
| doom-loop permission | 三个最近 tool part 同名同参 | 要求用户确认是否继续 | 不等同于符号冻结 |

对 `frozen` 的全仓检索只定位到 `packages/opencode/src/sync/index.ts` 的事件定义注册表冻结：
初始化后禁止继续定义 sync event，避免版本注册漂移。它与代码符号、已通过测试或 patch 范围无关。
`minimal change` 是 prompt 层工程原则，不是一个自动比较前后符号并拒绝写入的强制执行器。

### 10.2 NZ-Coder 原有状态与真实不足

A008 前，NZ-Coder 的保守修复基础已经比最初假设更完整：

- `RecoveryState` 对瞬态 API 错误最多重试三次并指数退避；
- 400/422 会注入 JSON/tool-call 诊断，而不是盲目重发；
- `old_text not found`、多重匹配和失败测试都会生成针对性恢复提示；
- 测试失败诊断要求读取 traceback、定位源文件、做最小根因修复并重跑最具体测试；
- 写批次由 TransactionManager 保护，dispatch failure 会回滚当前批次；
- verification gate 会阻止“改完但没有通过验证”时直接结束；
- SWE-bench 专用 `RetryOrchestrator` 已能读取 PASS_TO_PASS 回归、分析删除类/方法等 patch 风险，
  决定沿用旧 patch 还是从 clean checkout 重来，并注入 regression guard。

真实缺口位于通用 Agent Loop：如果模型连续三轮生成完全相同的工具名和参数，前两次结果已经
证明没有进展，默认第三次仍会照常进入权限检查和 dispatch。失败诊断只是建议模型换方法，没有运行时
防线保证它一定不会重复。无头运行和 benchmark 中也没有用户在场替它拒绝。

### 10.3 实现结果

核心调用链：

```text
每次 AgentLoop.run()
  -> reset_tool_call_history()
  -> 模型返回有序 tool calls
  -> 截取本轮允许执行的前缀
  -> 工具名 + 规范化 JSON 参数生成签名
     -> 相同签名：连续次数 +1
     -> 不同签名：新 streak 从 1 开始
  -> effective threshold = 0：关闭 guard
  -> count < effective threshold：进入 A007 调度、权限和 dispatch
  -> count >= effective threshold：构造 Denied ToolExecutionResult，不执行 handler
     （默认 threshold=3；正数配置最低按 2 处理）
  -> 正常 tool result 对应关系、trace 和 after-tool hook
  -> 注入 <doom-loop-diagnostic> 要求换方法并保持最小改动
```

参数使用排序后的规范 JSON 表示，因此 `{"path":"a","start":1}` 与键顺序相反的对象仍被
视为同一调用。无效 JSON 使用原始字符串参与签名，不会把三段不同的坏参数都误认为空字典。
任何不同工具或不同参数都会中断 streak；新的 `run()` 也会重置，避免上一条用户任务污染下一条。

达到阈值的那次调用（默认第三次）会在权限检查和 handler dispatch 之前被阻断，所以：

- 不会再次读取、写入、启动子任务或运行 shell；
- 仍生成对应 call id 的 tool result，provider 消息结构完整；
- `tool_calls_this_run` 只统计真正执行的调用；默认阈值 3 时只统计前两次；
- trace 记录 `doom_loop_blocked`、工具名、次数和阈值；
- after-tool recovery hook 把普通 Denied 输出升级成 `<doom-loop-diagnostic>`。

恢复诊断要求模型：不得再次原样提交同一调用；把已有输出和当前 workspace 当作事实；改变方法或缩小
参数；修复代码时保留 public API、已经通过的行为和无关文件；只有新证据才允许扩大编辑范围；
最后做最小、可验证的改动。

配置项为 `NZ_DOOM_LOOP_THRESHOLD`，默认 `3`；设为 `0` 关闭。为了避免“第一次调用就被
拒绝”这种无意义配置，正数阈值运行时最低按 `2` 处理。

### 10.4 关键设计决策

#### 硬阻断代替交互式 permission ask

InfCode 默认通过 `doom_loop` 权限向用户询问，适合有 GUI/TUI 人在回路的产品。NZ-Coder 同一
Agent Loop 还服务于 non-streaming、批量和无头运行；如果在第三次卡住等待输入，反而会把有限的
评测时间变成交互阻塞。本阶段选择确定性 Denied 结果，让模型立即获得恢复信号。代价是用户当前
不能对某一次重复调用点“仍然允许”；这是与 InfCode 的明确体验差异。

#### 比较语义参数，不比较原始 JSON 文本

模型可能只改变对象键顺序却没有改变调用含义。排序规范化能识别这种无效变化，也能让测试稳定。
对无效 JSON 则保留原始文本，避免不同解析错误被错误合并。

#### guard 位于 A007 调度之前

先识别阻断项，才决定并行 read segment 或 serial/write barrier。没有阻断时，A007 快路径和离线
并行性能不变；出现阻断时，该小批次走顺序结果组装，确保被拒调用不进入 worker。

#### 没有实现伪 frozen-symbol 强制器

仅凭“某次测试通过”无法安全推导一个函数、类或文件以后绝对不能修改；目标修复可能恰好需要改它。
若没有可证明的 baseline、symbol identity、变更原因和回归验证，硬冻结会制造假安全和误阻断。
本阶段沿用 NZ-Coder 已有的 PASS_TO_PASS guard、public API 提示、事务和 verification gate，只把
可客观证明的“连续同名同参且无任何中间动作”作为强制边界。

### 10.5 关键文件

- `nz_coder/runtime/recovery.py`：规范化签名、连续计数、run 重置和 doom-loop 恢复诊断。
- `nz_coder/runtime/loop.py`：同步/异步调度前 guard、Denied 结果、trace 和轻量构造兼容。
- `nz_coder/config.py`：`DOOM_LOOP_THRESHOLD` 环境配置。
- `.env.example`：阈值和关闭方式示例。
- `tests/test_recovery.py`：键顺序、阈值、不同调用重置、显式重置、关闭和诊断内容。
- `tests/test_loop_fake.py`：默认阈值下前两次执行、第三次阻断，以及不同参数中断 streak 的端到端测试。
- `README.md` 与 `docs/architecture.md`：用户可见行为、调度位置和安全模型。

### 10.6 验证结果

完成本项时：

- `tests/test_recovery.py` + 完整 `tests/test_loop_fake.py`：`42 passed`；
- 加上并行 benchmark/runtime context 回归：`53 passed`；
- 完整测试套件：`394 passed`；
- 只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- 涉及文件 `py_compile` 通过；
- 聚焦 Ruff 检查通过，沿用项目对历史 F401/E402/F811 的既有忽略；
- 默认阈值下的离线端到端用真实 `list_directory` handler 验证：前两次执行，第三次返回 Denied，随后模型可继续；
- 没有发送模型请求，没有运行 SWE-bench 官方评测。

关键断言：

- `test_observe_tool_call_counts_canonical_equivalent_arguments`：JSON 键顺序不能绕过 guard；
- `test_observe_tool_call_resets_after_different_call`：不同调用中断连续 streak；
- `test_reset_tool_call_history_starts_a_fresh_streak`：新 run 可从干净状态开始；
- `test_identical_third_tool_call_is_blocked_before_dispatch`：默认阈值下第三次不执行但 call id/result 完整；
- `test_different_tool_call_breaks_identical_call_streak`：参数确实改变后不会误阻断；
- A007 并行 benchmark 与 runtime context 测试：轻量 `AgentLoop.__new__()` 构造仍兼容。

复现命令：

```bash
python3 -m pytest -q tests/test_recovery.py tests/test_loop_fake.py
python3 -m pytest -q tests/test_parallel_benchmark.py \
  tests/test_runtime_context.py tests/test_recovery.py tests/test_loop_fake.py
python3 -m pytest -q tests/test_loop_fake.py \
  -k 'identical_third_tool_call or different_tool_call_breaks'
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/config.py nz_coder/runtime/recovery.py nz_coder/runtime/loop.py \
  tests/test_recovery.py tests/test_loop_fake.py
python3 -m py_compile nz_coder/config.py nz_coder/runtime/recovery.py \
  nz_coder/runtime/loop.py tests/test_recovery.py tests/test_loop_fake.py
python3 -m pytest -q
```

### 10.7 学习重点

1. 先证明竞品真实存在什么，再设计对齐项；源码里的 `frozen` 不一定是 frozen symbol。
2. API retry、tool-call repair 和业务修复 retry 是不同层，不能用一个“重试”概念混在一起。
3. prompt 建议与 runtime guard 的可靠性不同；无头 Agent 需要确定性停止无进展动作。
4. 判断“相同调用”应比较规范化语义输入，同时保留坏 JSON 的原始差异。
5. 保守修复不是一律禁止修改，而是只在证据不足时阻止扩大范围，并要求验证已保留行为。
6. 新 guard 必须兼容并行调度、事务、call id/result 协议和测试中的轻量构造路径。

### 10.8 设计边界与剩余差距

当前接受的边界：

- NZ-Coder 在达到配置阈值时直接 Denied（默认阈值 3，即默认第三次），没有 InfCode 那样的 ask/always 权限交互和本次覆盖入口；
- 只识别连续、完全同名同参调用；`grep A -> grep B -> grep A` 不算 doom loop；
- 参数只要有任何值变化就重置，guard 不判断两个不同 query 是否语义上同样无效；
- 阻断次数不会持久化到 `/resume`，每个新的 Agent run 都重新开始；
- minimal-diff/public-API 约束通过诊断和现有验证链传递，不是通用 patch 拒绝器；
- 没有基于 AST/LSP 的 symbol freeze，也没有声称与 InfCode 存在这项对齐；
- SWE-bench 的 regression guard 仍是 benchmark 专用，没有自动推广到所有普通修复任务。

后续可继续对齐：

- 为交互式 CLI 增加可选 doom-loop 覆盖确认，同时让无头模式保持确定性阻断；
- 识别交替循环或连续“无结果搜索”模式，但必须控制误报；
- 把通用 patch 风险摘要接入普通修复 replan，而不是复制 benchmark 专用规则；
- 增加 per-call duration、blocked reason 和 streak reset trace，便于量化卡死减少情况；
- 用真实 Agent trace 比较阻断前后重复工具次数和任务完成率。

## 11. A009：分层验证管线与证据代际

A009 不是一项一对一功能移植。它的结论与最初候选名称有一个重要差异：InfCode 当前并没有运行时强制的
“静态检查 → 目标测试 → 回归测试”执行器。它依赖模型 prompt 主动选择 Bash 验证，编辑工具提供
即时 LSP/config 诊断，会话层记录工具状态和 patch，但完成条件不读取测试结果。NZ-Coder 因而不是
复制一个不存在的 InfCode 模块，而是保留其“编辑后反馈、模型主动验证”的交互方式，并把自己已有的
completion gate 升级为可审计的分层状态机。

### 11.1 InfCode 参考能力

本项复核基于当前本地 InfCode 源码快照，重点事实如下：

- `packages/opencode/src/session/prompt/default.txt` 要求模型在可能时运行测试，并在项目提供命令时
  执行 lint/typecheck；`gemini.txt` 给出 Understand → Plan → Implement → Tests → Standards 的
  推荐顺序；`copilot-gpt-5.txt` 强调频繁测试和全部通过；
- `packages/opencode/src/tool/bash.ts` 是由模型显式调用的通用 shell 工具，只把退出码写入
  `metadata.exit` 和输出，没有把失败升级成会话完成门禁；
- `tool/edit.ts`、`write.ts`、`apply_patch.ts` 在写后执行格式化、IDE 刷新、文件事件、LSP 诊断和
  特定配置校验；诊断错误会附加到工具输出，工具本身仍可成功返回；
- `session/processor.ts` 记录工具的 pending/running/completed/error 状态，在 step start/finish 和
  cleanup 阶段记录 snapshot/patch part；
- `session/prompt.ts` 在 assistant 得到终止 finish、没有 tool call 等条件满足时结束，未发现
  “有编辑但没有测试”或“测试失败所以拒绝结束”的分支。

因此，InfCode 的优势是编辑即时反馈、完整会话记录和针对模型的验证约束；它没有提供可直接复制的
统一分层验证 gate。本项必须明确写成 NZ-Coder 的可靠性增强，不能声称与 InfCode 逐行等价。

### 11.2 NZ-Coder 原有不足

A009 前，NZ-Coder 已有以下基础：

- `VerificationManager` 在实质写入后打开 completion gate；
- `verify_changed_files` 可运行 Python `py_compile`、JS/TS typecheck、Go compile-only 和
  Rust `cargo check` 等低噪检查；
- `plan_verification` 能推荐 `py_compile`、明确失败测试、同名相关测试和宽测试 fallback；
- `RunEvidence` 与 reflection reviewer 能保存并复核结构化验证证据；
- RuntimeState 区分精确测试和宽测试次数，SWE-bench 另有官方 harness 回归策略。

真实缺口不是“没有测试命令”，而是这些能力没有形成同一个状态模型：

1. planner 只有 flat `recommended/fallback/notes`，机器不知道命令属于哪一层；
2. 任意一个被识别的验证命令成功都会清除整个 gate，一个文件的 `py_compile` 可以掩盖其他文件或
   已知目标测试；
3. 一个阶段失败后，另一阶段成功可能覆盖 `_needed`，丢失未解决失败；
4. 普通 Bash `pytest` 不进入 `RunEvidence.verification_results`；
5. 如果简单追加失败和重跑成功，reviewer 的历史 `any(failed)` 又会让旧失败永久阻塞；
6. 旧命令识别使用 substring，`echo pytest`、`rg pytest` 也可能被误认为真正验证；
7. gate 会展示宽测试 fallback，但产生 diff 后的命令策略可能阻止宽测试，容易形成无效建议循环。

### 11.3 实现结果

新的核心调用链：

```text
成功的实质写工具
  -> 提取单文件、batch 或 patch 中的 changed_files
  -> 惰性构建 plan_verification_commands()
  -> 保留 recommended/fallback/notes，并新增有序 stages
     static     -> 可推导的语法/类型/编译检查，逐命令 required
     targeted   -> 明确失败测试 required；文件名猜测的相关测试 optional
     regression -> 宽测试 optional，不由 gate 强制
  -> bash / python_symbol_check / verify_changed_files 返回
  -> 共享分类器识别真实 executable 和 stage
  -> 更新具体 command 状态
  -> 所有 required command 通过且没有未解决失败，才清除 gate
  -> status/trace/RunEvidence 暴露阶段结果
```

planner 的旧返回字段完全保留，新增结构为：

```python
{
    "recommended": [...],
    "fallback": [...],
    "notes": [...],
    "stages": [
        {
            "name": "static",
            "required": True,
            "commands": [
                {"command": "python -m py_compile pkg/a.py", "reason": "...", "required": True}
            ],
        },
        {"name": "targeted", "required": False, "commands": [...]},
        {"name": "regression", "required": False, "commands": [...]},
    ],
}
```

主要运行语义：

- static 层按命令覆盖；两个变更 Python 文件产生两个 required `py_compile`，只通过一个不能清门；
- `verify_changed_files OK` 是 static 聚合结果，可以一次完成该层；带 `path` 的
  `python_symbol_check OK` 只覆盖该文件对应的 static command，不能替其他变更文件通过；缺少路径时保留
  原有聚合兼容行为；
- 如果存在可执行 static command，`WARN` 不会假装这些命令已经通过；未知项目没有 required command
  时仍保留原有 skipped/legacy 降级，避免死锁；
- 修复前观察到的 pytest 失败 ID 会跨编辑转成 required targeted command；没有明确失败证据时，按文件名
  猜出的相关测试只是 optional；
- regression 默认 optional，`include_broad=True` 只影响工具输出放在 recommended 还是 fallback，
  不会偷偷改变 completion gate 的 required 语义；
- optional 检查不运行不阻塞；一旦主动运行并失败，在当前计划内保持失败，不能被 static 成功掩盖；
  新写入会重建计划，明确失败目标仍会延续；
- planner 异常或无法生成 stage 时退回旧的“至少一个真实验证结果”语义，通用项目不会永久卡住；
- `last_status` 新增兼容字段 `verification_pipeline`，原有 `verification_needed` 和
  `last_verification` 不变；gate 不会建议尚未运行的 optional command。它会列出 pending/failed
  required command；如果 optional command 已经运行且失败，也会重列该失败命令，要求重跑或在新写入后
  重建计划。

用于验证证据的命令分类器先分解 shell segment、去除 env/`uv run`/`poetry run`/`pipenv run` 等 wrapper，识别
真实 executable；组合命令中的每个真实验证 segment 都分别更新对应 stage，单值兼容接口仍使用
regression > targeted > static 的优先级。它能识别
`python -m pytest`、`go test -run`、Cargo、Node、Maven/Gradle 等常见命令，同时拒绝把
`echo pytest`、`printf 'pytest'`、`rg pytest` 当成验证。required command 的覆盖也限制在实际执行的
单个 shell segment，打印计划命令再运行另一静态工具不能伪造通过。语义 key 会忽略 quiet/verbose 等
展示参数，并统一 runner wrapper 与 Go 参数顺序，因此失败后以等价命令重跑成功可以替换旧结果。

分类器还明确不把会修改工作区或不能证明正确性的命令当作“验证通过”证据：`ruff format`、`ruff check --fix`、
`eslint --fix`、`biome check --write`、缺少 `--noEmit` 的 `tsc`，以及不含断言的任意
`python -c`。手写 Python probe 若输出明确的 `FAIL`、traceback 或 `No module named`，仍可提供失败证据，
但仅仅退出 0 不能清除 gate。这个分类器只判断验证证据，不是通用 Shell 安全引擎，也不负责拦截这些命令。

RunEvidence 同步升级：

- 普通 Bash 验证、`python_symbol_check`、`verify_changed_files` 和 `verify_project_build` 统一写入
  `stage`；非验证 shell 不进入 verification results；
- 相同 stage + command 使用 latest-result upsert，失败后同命令重跑成功不会残留永久失败；
- 验证命令的业务失败不再重复记成永久 tool failure；真正的 Error/Denied dispatch failure 仍保留；
- 新的实质代码写入会清除上一代 diff 的 verification/build evidence，防止旧 pass 或 fail 错绑到新代码；
  只有通过 `write_file` 写入根目录 `.md`/`.txt`/`.rst` 文档时沿用既有轻量豁免，`edit_file`、
  `apply_patch` 以及子目录文档写入仍会使证据失效；完整尝试历史保留在 trace。
- 删除的 Python 文件不再生成不可执行的 per-file `py_compile`；`verify_changed_files` 将其记录为
  `SKIP ... (deleted file)`，其他仍存在的 required 文件照常逐项验证。

### 11.4 关键设计决策

#### 只规划和门控，不后台自动执行

InfCode 由模型显式调用 Bash，NZ-Coder 也保留这个授权边界。planner 不执行命令，gate 只说明必须补齐
哪些证据，避免 Agent 在用户不知情时启动昂贵、联网或有副作用的项目脚本。

#### 明确失败目标 required，启发式相关测试 optional

pytest 已报告的精确 test ID 是强证据；文件名相似只是弱猜测。把弱猜测设为 required 容易因仓库命名
不规则卡死 gate，所以它只作为建议。宽回归同样默认 optional，既控制成本，也兼容 SWE-bench 把
PASS_TO_PASS 留给官方 harness 的策略。

#### stage 内按命令完成，不用“任一成功”

阶段标签只能说明验证类型，不能证明覆盖范围。static 层必须追踪每个 changed source command；聚合工具
只有在自身确实检查整组变更时才能 stage-wide 通过。这解决了一个文件成功掩盖其他文件的核心问题。

#### 当前证据代替无限历史

trace 负责保留尝试历史，RunEvidence/reviewer 需要回答“当前 diff 是否有有效证据”。因此同命令重跑用
最新结果覆盖，实质新写入使上一代验证失效。否则旧失败会永久拒绝完成，旧成功也会错误证明新代码。

#### 不复制 InfCode 不存在的质量门

InfCode 的 LSP 诊断和 snapshot/patch 记录仍值得学习，但不能把 prompt 建议描述成 runtime enforcement。
本项选择在 NZ-Coder 已有 gate 上增强，是有意超过当前 InfCode 完成条件，而非错误宣称一比一复刻。

### 11.5 关键文件

- `nz_coder/intelligence/verification_planner.py`：三层 plan、required/optional 语义和验证命令识别；
- `nz_coder/intelligence/verification.py`：changed-file 快照、命令级 stage 状态、失败目标延续和 gate 输出；
- `nz_coder/run_evidence.py`：stage 证据、latest upsert、写后证据失效和验证失败/工具失败分离；
- `nz_coder/runtime/loop.py`：沿用现有观察协议，仅把 `python_symbol_check` 的 path 输入继续传给 VM；
- `nz_coder/tools/repo_intel.py`：聚合静态验证跳过已删除的 Python 文件；
- `tests/test_verification_planner.py`：plan 兼容、阶段策略、wrapper/误判分类矩阵；
- `tests/test_verification.py`：顺序、逐命令覆盖、聚合结果、失败粘性、legacy fallback 和提示边界；
- `tests/test_run_evidence.py`：bash stage、upsert、非验证过滤和 diff 证据代际；
- `tests/test_loop_fake.py`：static 后提前结束被拉回、target 通过后完成的真实工具端到端链路；
- `README.md` 与 `docs/architecture.md`：用户行为和模块职责说明。

### 11.6 验证结果

完成本项时：

- verification/planner/evidence/Loop/Repo/command-policy/runtime-context 聚焦回归：`235 passed`；
- 完整测试套件：`491 passed`；
- 只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- 涉及 Python 文件 `py_compile` 通过；
- 聚焦 Ruff 通过，沿用项目对历史 F401/E402/F811 的既有忽略；
- 临时真实 workspace 中实际执行 `python -m py_compile app.py` 与
  `python -m pytest -q tests/test_app.py::test_run`：只完成 static 时模型结束被 gate 拉回，target 通过后
  status 为 completed，RunEvidence 分别记录 static/targeted；
- 没有发送模型请求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q tests/test_verification_planner.py \
  tests/test_verification.py tests/test_run_evidence.py \
  tests/test_loop_fake.py tests/test_repo_intel.py tests/test_command_policy.py \
  tests/test_smoke.py tests/test_runtime_context.py
python3 -m pytest -q tests/test_loop_fake.py \
  -k 'verification_after_writes or required_static_and_targeted'
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/intelligence/verification.py \
  nz_coder/intelligence/verification_planner.py nz_coder/run_evidence.py \
  nz_coder/runtime/loop.py nz_coder/tools/repo_intel.py \
  tests/test_verification.py tests/test_verification_planner.py \
  tests/test_run_evidence.py tests/test_loop_fake.py tests/test_repo_intel.py
python3 -m py_compile nz_coder/intelligence/verification.py \
  nz_coder/intelligence/verification_planner.py nz_coder/run_evidence.py \
  nz_coder/runtime/loop.py nz_coder/tools/repo_intel.py
python3 -m pytest -q
```

### 11.7 学习重点

1. prompt 要求、编辑即时诊断、验证证据和 completion gate 是四个不同层次，不能混为一个“会测试”。
2. 分层标签不等于覆盖证明；stage 内仍需追踪具体 required command。
3. 强证据和弱启发式必须区分：明确失败测试可强制，文件名猜测不能轻易强制。
4. 当前状态与历史 trace 的用途不同；reviewer 应看当前 diff 的最新证据，trace 才保存完整尝试过程。
5. shell 命令分类必须识别真实 executable，substring 会被日志、搜索和 echo 轻易误导。
6. 宽测试策略必须与 gate 建议一致，不能一边提示 bare pytest、一边在 diff 后由 policy 阻断。
7. 对齐竞品时也允许保留本项目更强的已有机制，但必须明确哪些是参考、哪些是自主增强。

### 11.8 设计边界与剩余差距

当前接受的边界：

- 不自动执行任何验证命令；最终质量仍依赖模型调用工具并遵守 gate；
- 三层的顺序是计划、展示和审计顺序，不是后台 executor 的强制运行时序；若模型先跑 targeted，结果会被
  记录，但 completion 仍要求所有 required command 通过；
- regression 默认 optional，没有按风险等级自动升级为 required；
- changed-file 关联是路径列表和 evidence generation，不是 git tree/diff hash 指纹；
- 精确失败目标的自动延续目前对 pytest 最完整，unittest/Go/Cargo 的失败输出解析仍可扩展；
- 共享分类器覆盖常见 runner，但自定义脚本只能通过 planner 精确命令或 legacy fallback 识别；
- completion gate 最多仍提示 `MAX_VERIFICATION_GATE_PROMPTS` 次，之后可返回 `completed_unverified`；
- 用户显式 `continue/继续/keep going` 仍可触发既有 gate bypass，这是人工覆盖入口；
- `RunEvidence` 本身仍是观察结构，真正的控制流门禁位于 VerificationManager/hook；
- RunEvidence 在单个成功写工具后立即切换证据代际；同批事务若随后失败并回滚，证据代际尚不会随事务
  一起恢复；
- reviewer 仍使用 flat evidence 判断 pass/fail，没有独立读取 VerificationManager 的 required-stage 快照；
- 本项没有增加 formatter/fixer 拦截，也没有把任意 Bash 写操作接入文件事务；Shell 写入跟踪属于独立的
  安全/事务课题，不再放进 InfCode 验证对齐范围；
- InfCode 在编辑工具内直接附加 LSP/config 诊断，NZ-Coder 的 A001 写后诊断链路仍是独立实现，未在本项重做。

后续可继续：

- 按 diff 风险、任务明确要求和执行预算决定何时将 regression 升级为 required；
- 为 unittest、Go、Cargo、Jest/Vitest 增加精确失败 ID 提取和重跑命令生成；
- 给 verification generation 增加稳定 diff fingerprint，防止 resume/外部编辑后的证据错绑；
- 让 reviewer 直接消费 pipeline snapshot，区分 unavailable、skipped、pending 和 deferred-to-harness；
- 统计每层耗时、失败率、重跑次数和 broad-test defer 原因。

## 12. A010：结构化用户提问工具

### 12.1 InfCode 参考能力

本项复核基于当前本地 InfCode 源码快照。`infcode-dev/infcode-dev` 本身没有可用于确认来源版本的
`.git` 元数据；此前从该目录执行 Git 命令时，Git 实际向上找到了 NZ-Coder 父工作区，因此得到的提交号
不能作为 InfCode 的版本证据。下面只记录本地源码可以直接证明的行为，不声称它们属于某个上游提交。
主要参考：

- `packages/opencode/src/tool/question.ts`：注册模型可调用的 `question` 工具，把回答格式化回工具结果；
- `packages/opencode/src/tool/question.txt`：只在无法从请求、代码或合理默认值解决的用户决策上提问；
- `packages/opencode/src/question/index.ts`：pending question、reply/reject、事件发布和等待回答；
- `packages/opencode/src/question/schema.ts`：问题、选项、单选/多选和回答 schema。

InfCode 面向模型的工具一次接受 1–4 个问题；每题有短 `header`、完整问题、2–5 个选项和可选
`multiple`。用户始终可以输入自定义答案，因此模型不应手工添加 `Other`。如果有推荐项，应放在首位并在
label 后标记 `(Recommended)`。

### 12.2 NZ-Coder 原有不足

A010 前，NZ-Coder 只有两种相近但不同的交互：

- `PermissionManager.ask_user()` 只回答某个工具是否允许执行；
- 子 Agent 的 `message_parent` 只能把问题交回父 Agent，不能直接向终端用户展示结构化选项。

主 Agent 没有通用澄清工具。模型遇到真正由用户决定的存储后端、兼容性范围或破坏性迁移选择时，只能在
普通文本中停下；工具循环无法把问题、答案和后续继续执行放进同一条 tool-call 链路。

### 12.3 实现结果

核心调用链：

```text
AgentLoop.run()
  -> 用 scoped_question_asker() 绑定本会话回调
  -> 模型调用 question({questions: [...]})
  -> handler 再校验 1–4 题、2–5 选项、header、label 和 multiple
  -> CLI adapter 暂停 StreamingRenderer
  -> 显示编号选项，读取单选/逗号多选或自定义文本
  -> finally 恢复 StreamingRenderer
  -> 答案作为普通 tool result 写回消息历史
  -> 模型在下一轮继续任务
```

主要行为：

- `question` 默认随 AgentLoop 加载，执行效果为 `serial`，因此不会与相邻只读工具并发争抢终端输入；
- 回调通过 `ContextVar` 和 AgentLoop 的 `ExitStack` 作用域绑定，不使用跨会话全局单例；
- CLI 复用现有 renderer 的 `pause()`/`resume()`；输入 `1` 选择单项，`1,2` 选择多项，非数字文本作为
  自定义答案，空输入或 Ctrl-C/EOF 视为 dismiss；
- dismiss 是正常工具结果，提示模型采用最佳判断并说明假设，不触发工具调度失败；
- 无 renderer、GUI adapter 或显式 `question_asker` 的无头模式立即返回 `Error: Interactive question
  service unavailable`，不会读取 stdin 或无限等待；
- 主 Agent 的 permission policy 将 `question` 视为安全串行工具；子 Agent 不暴露它，仍通过
  `message_parent` 由父 Agent 统一协调用户交互；
- prompt 明确限制：只有用户答案会实质改变下一步、且代码和合理默认值都无法解决时才使用，不能拿它询问
  “是否继续”。

### 12.4 关键设计决策

#### 先对齐主 Agent 的最小交互闭环

InfCode 有事件总线、pending map、GUI question card、reply/reject API 和持久化 session part。NZ-Coder 当前
主入口是本地终端 REPL，直接复制整套服务层会把一次工具对齐扩大成 UI/服务端重构。本阶段只实现
tool-call → 暂停终端 → 回答 → tool-result 这一条可测试链路。

#### 回调注入而不是工具内直接 `input()`

handler 内直接调用 `input()` 会让 benchmark、API 客户端和测试无头挂起，也会破坏 Rich Live 画面。回调
注入让 CLI 提供终端实现，未来 GUI/API 可以提供自己的 adapter；没有 adapter 时确定性失败。

#### 子 Agent 不直接占用用户交互通道

多个子 Agent 若同时提问，会产生回答归属和终端争用问题。A007 已规定副作用屏障，A010 进一步只允许主
Agent 使用 question；子 Agent 继续用 `message_parent` 请求父 Agent 决策。

### 12.5 关键文件

- `nz_coder/tools/question.py`：schema、运行时校验、会话级回调和工具结果格式；
- `nz_coder/interface/questions.py`：终端显示、选择解析以及 renderer 暂停/恢复；
- `nz_coder/runtime/loop.py`：副作用 import、构造注入和每次 run 的 context-local 绑定；
- `nz_coder/runtime/prompt.py`：何时允许提问的行为约束和工具说明；
- `nz_coder/runtime/subagent.py`：禁止子 Agent 直接调用 question；
- `nz_coder/tool_platform/permissioning/tool_groups.py`：主 Agent 安全工具分组；
- `tests/test_question.py`：schema、回调、dismiss、无头、终端解析、权限和子 Agent 隔离；
- `tests/test_loop_fake.py`：模型调用 question、答案回填、下一轮继续的 fake-client 端到端测试。

### 12.6 验证结果

完成本项时：

- question/Loop/smoke/permissions/runtime-context/subagent 聚焦回归：`128 passed`；
- 完整测试套件：`508 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- A010 涉及的 Python 文件 `py_compile` 通过；
- A010 源码与新增测试 Ruff 通过；`tests/test_smoke.py` 仍有两处与本项无关的既有 F541/E401，因此未把
  该历史文件纳入本项 Ruff 命令；
- fake terminal 覆盖无效编号后重试、renderer 恢复和推荐项选择；fake AgentLoop 覆盖 tool result 回填；
- 没有发送真实模型请求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q tests/test_question.py tests/test_loop_fake.py \
  tests/test_smoke.py tests/test_permissions.py tests/test_runtime_context.py \
  tests/test_subagent.py
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/tools/question.py nz_coder/interface/questions.py \
  nz_coder/runtime/loop.py nz_coder/runtime/prompt.py \
  nz_coder/runtime/subagent.py \
  nz_coder/tool_platform/permissioning/tool_groups.py \
  tests/test_question.py tests/test_loop_fake.py
python3 -m py_compile nz_coder/tools/question.py \
  nz_coder/interface/questions.py nz_coder/runtime/loop.py \
  nz_coder/runtime/prompt.py nz_coder/runtime/subagent.py
python3 -m pytest -q
```

### 12.7 学习重点

1. 权限确认回答“能不能执行工具”，需求澄清回答“用户想要哪种结果”，两者不能共用一个布尔接口。
2. 任何可能等待 stdin 的工具都必须有明确的无头行为，否则评测或 API 服务会永久阻塞。
3. 结构化提问的价值不只是 UI，而是把问题与答案纳入 tool-call 历史，使模型能在同一 run 中继续。
4. 会话级交互服务应通过作用域注入隔离；模块级可变 callback 会在并发 AgentLoop 间串线。
5. “能问问题”不等于“应该多问问题”；工具描述和 system prompt 必须共同抑制可由代码或默认值解决的提问。

### 12.8 设计边界与剩余差距

当前接受的边界：

- 只有终端 adapter 和构造参数 `question_asker`，尚无 PySide/HTTP/IDE question card；
- 没有 InfCode 的 pending-question 列表、事件总线、request ID、reply/reject API 或数据库状态；
- 回答通过普通会话消息持久化，没有独立的结构化 question summary part；
- 当前终端只接受编号、逗号多选或整段自定义文本，不支持方向键、复选框或鼠标交互；
- dismiss 后由模型采用最佳判断，不会暂停整个 session 等待日后恢复；
- 工具 schema 由注册表提供，handler 仍做二次校验，因为不同 OpenAI-compatible provider 对 JSON Schema
  约束的遵守程度不同；
- 子 Agent 不能直接提问，父 Agent 需要根据 `message_parent` 自行决定是否向用户调用 question。

后续如果真实客户端需要，可继续：

- 为 PySide/HTTP/IDE 增加异步 question adapter 和 request ID；
- 把 pending/reply/reject 状态持久化到 session runtime，使断线后可恢复；
- 在 trace 中增加 dismissed、answer source 和等待耗时，同时避免记录敏感答案正文；
- 增加客户端 capability 协商，使模型在无交互 adapter 时不暴露 question schema。

## 13. A011：Plan/Build 模式闭环

### 13.1 InfCode 参考能力

本项同样只以当前本地 InfCode 源码快照为证据，不绑定 Git 提交。主要参考：

- `packages/opencode/src/tool/plan-enter.txt`：复杂、多文件或用户明确要求先规划时建议切入 Plan；简单任务和
  明确要求立即实现的任务不应切换；
- `packages/opencode/src/tool/plan-exit.txt`：计划文件完成、需求问题已澄清后，用 `title` 和 Markdown
  bullet `summary` 表示计划已可评审；
- `packages/opencode/src/tool/plan.ts`：`plan_exit` 读取专属计划文件，返回相对路径、标题、摘要和内容指纹；
- `packages/opencode/src/session/prompt/plan.txt`：Plan 阶段只读，唯一写入例外是计划文件，最终必须提问或
  调用 `plan_exit`；
- `packages/opencode/src/agent/agent.ts`：Build 允许 `plan_enter`，Plan 禁止普通 edit、只允许写项目内的
  plan 路径并允许 `plan_exit`；
- `packages/opencode/src/kilocode/plan-followup.ts`：`plan_exit` 后由用户继续、修改或取消；如果用户在
  审批期间改了计划文件，选择开始实现后 code agent 会以最新内容为准并重新读取；
- `packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`：完成 `plan_enter` 后切换到 plan agent，
  `plan_exit` 本身不直接切换，由后续审批流程接管。

本地快照展示的是一套“Agent profile + 权限 + 计划文件 + UI 审批”的产品闭环。真正需要对齐的语义不是
GUI 卡片样式，而是以下状态边界：

```text
Build
  -> 请求进入 Plan
  -> 用户同意
Plan（源码只读，计划文件可写）
  -> 完成计划并调用 plan_exit
  -> 用户评审
  -> 同意后进入 Build，并以审批时的最新计划为准
  -> 拒绝、dismiss 或给出修改意见则继续 Plan
```

### 13.2 NZ-Coder 原有不足

A011 前，NZ-Coder 已有 `default`、`auto`、`plan` 和 `acceptEdits` 四种 PermissionManager 模式；其中
`plan` 已能拒绝文件写工具、变更型 Bash 和未知 Shell 命令，只放行读取操作。它还已有 A010 的结构化
终端提问适配器。

缺少的是把这些零散能力连成工作流的控制面：

- 模型没有 `plan_enter`，不能根据任务复杂度请求切换；
- Plan 模式会禁止所有写操作，没有“只允许计划文档”的受控例外；
- 没有固定、可供用户打开评审的 session 计划文件；
- 模型没有 `plan_exit`，无法明确区分“还在想”与“计划已完成”；
- 用户批准后何时恢复 Build 没有工具批次级安全边界；
- 计划审批期间如果用户手工改了文件，Agent 不知道摘要已经过期；
- 每轮模型请求的 system prompt 不会告诉模型当前处于只读规划阶段。

因此，原来的 `/mode plan` 只是手工权限开关，不是 InfCode 意义上的 Plan/Build 交互闭环。

### 13.3 实现结果

核心调用链：

```text
模型调用 plan_enter(reason)
  -> 复用 A010 question_asker 展示“进入 Plan / 继续 Build”
  -> 用户同意后清空本 session 专属计划文件
  -> PermissionManager.mode = plan
  -> 下一轮把 <plan-mode> 边界附加到 system prompt

Plan 阶段
  -> read/search/LSP/只读 Bash 正常工作
  -> 普通文件写和变更型 Bash 被 PermissionChecker 拒绝
  -> write_plan(content) 原子替换 .nz-coder/plans/<session-id>.md

模型调用 plan_exit(title, summary)
  -> 确认计划文件存在且非空
  -> 记录 SHA-256 内容指纹
  -> 用户选择“Approve Plan / Keep Planning”
  -> 批准后重新读取计划并比较指纹
  -> 只登记 pending Build mode，不立即解锁写权限
  -> 当前整批工具调用结束
  -> AgentLoop.apply_pending_mode()
  -> 恢复进入 Plan 前的 default / auto / acceptEdits 模式
```

状态变化如下：

| 当前状态 | 事件 | 结果 |
|---|---|---|
| Build | 用户拒绝或 dismiss `plan_enter` | 保持原模式，不创建计划文件 |
| Build | 用户同意 `plan_enter` | 清空本 session 计划文件，进入 Plan |
| Plan | 普通源码写入 | PermissionManager 拒绝 |
| Plan | `write_plan` | 仅原子写固定计划路径 |
| Plan | 空计划调用 `plan_exit` | 返回 `Error:`，继续 Plan |
| Plan | 用户拒绝或 dismiss 审批 | 继续 Plan，不产生 pending 解锁 |
| Plan | 审批期间计划文件变化 | 指纹不一致，要求重新读取和审批 |
| Plan | 用户批准 | 当前批次仍保持 Plan；批次完成后恢复此前 Build 模式 |

这里专门测试了一个容易忽略的越权场景：模型在同一个响应里依次调用 `plan_exit` 和 `write_file`。
`plan_exit` 虽然已经得到用户批准，但只登记延迟切换；紧随其后的 `write_file` 仍在 Plan 权限下执行并被
拒绝。等该批次所有 tool result 都已生成，AgentLoop 才恢复 Build。这样用户批准的是“下一阶段可以实现”，
不是“本批未审查的后续工具立即获得写权限”。

### 13.4 关键设计决策

#### 复用 PermissionManager，不复制第二套 Agent profile

InfCode 用 build agent 与 plan agent 的不同 permission ruleset 表达阶段。NZ-Coder 已有可变的
PermissionManager mode，如果再实现一套独立 agent registry，会制造两份权限真相。本阶段让
`PlanModeController` 只保存进入前模式和 pending transition，实际读写判断仍由现有 PermissionChecker
完成。

#### 专属 `write_plan` 是控制面写入，不是通用源码写工具

`write_plan` 不接受 path 参数，路径固定由 session ID 推导为
`.nz-coder/plans/<session-id>.md`。每次访问仍通过 `_safe_path()` 验证不能逃逸 workspace，内容使用同目录
临时文件、`fsync()` 和原子替换写入。它注册为 `serial` 状态工具，因此形成并发屏障，但不会触发源码
ChangeTracker、验证代际或 LSP 诊断。

这比在 Plan 模式中给通用 `write_file` 增加路径通配例外更小、更容易审计：模型无法把另一个文件伪装成
计划文件路径，也不会把 session artifact 当成待验证源码。

#### 进入和退出都复用 A010 的交互适配器

工具 handler 不直接调用 `input()`。CLI 继续负责暂停/恢复 renderer；构造时注入的 adapter 可以由未来
GUI/API 替换。无交互服务时立即返回 `Error:`，不会让无头运行或评测卡在 stdin。

Plan 工具只对主 Agent 暴露，所有子 Agent 均隐藏 `plan_enter`、`write_plan` 和 `plan_exit`。子 Agent 的
`plan` 类型仍表示只读设计任务，不应改变父会话的交互模式。

#### `plan_exit` 是审批边界，不是普通 question

系统提示明确要求：需求或方案未确定时用 `question` 澄清；计划是否批准只能通过 `plan_exit`。这样模型
不会绕过计划文件和内容指纹，拿一个普通 Yes/No question 提前进入实现。

#### 没有复制 InfCode 的 GUI、事件总线和独立 plan agent 服务

NZ-Coder 当前产品入口是终端 REPL。A011 只实现对正确性有影响的状态机、计划文件、审批与权限时序，没有
复制 plan summary card、数据库 session part、事件订阅或多客户端 request ID。这些属于后续客户端能力，
不应阻塞最小可用闭环。

审批期间计划被手工修改时，两者还有一个有意保留的差异：本地 InfCode 在用户选择开始实现后切换到 code
agent，并提示它重新读取最新计划；NZ-Coder 的终端审批没有可同时编辑的 plan card，因此采取更保守的
策略——检测到指纹变化就留在 Plan，要求重新读取并再次 `plan_exit`。本文不把这一点描述成与 InfCode
完全相同。

### 13.5 关键文件

- `nz_coder/tools/plan_mode.py`：PlanModeController、三项工具、原子计划文件写入、审批和内容指纹；
- `nz_coder/state/sessions.py`：生成 workspace 内固定 session 计划路径；
- `nz_coder/runtime/loop.py`：工具注册、controller 构造与作用域绑定、动态 Plan prompt、批次后模式切换；
- `nz_coder/runtime/prompt.py`：何时进入 Plan、Plan 阶段行为和三个工具说明；
- `nz_coder/tool_platform/permissioning/tool_groups.py`：把受控 Plan 工具列为主 Agent 安全控制面工具；
- `nz_coder/runtime/subagent.py`：所有子 Agent 隐藏 Plan/Build 切换工具；
- `tests/test_plan_mode.py`：注册、权限、无头、进入/拒绝、原子计划文件、审批、内容变化和同批越权测试。

### 13.6 验证结果

完成本项时：

- Plan/Question/Smoke/Fake Loop 聚焦回归：`107 passed`；
- 完整测试套件：`517 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- A011 全部修改文件 `py_compile` 通过；
- 新增模块、session、subagent、permission group 和新增测试的严格 Ruff：`All checks passed!`；
- 包含历史导入布局的全部修改文件使用项目既有兼容忽略项 `F401,E402,F811` 后，Ruff：
  `All checks passed!`；不把 `loop.py` 中原有文件中段 import 和重复 `_futures` 记为 A011 新问题；
- fake AgentLoop 真实走过 `plan_exit` 后同批 `write_file`，确认写入被拒绝且批次结束后才切回 Build；
- 没有发送真实模型请求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q tests/test_plan_mode.py tests/test_question.py \
  tests/test_smoke.py tests/test_loop_fake.py
python3 -m ruff check \
  nz_coder/tools/plan_mode.py nz_coder/state/sessions.py \
  nz_coder/runtime/subagent.py \
  nz_coder/tool_platform/permissioning/tool_groups.py \
  tests/test_plan_mode.py
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/tools/plan_mode.py nz_coder/state/sessions.py \
  nz_coder/runtime/loop.py nz_coder/runtime/subagent.py \
  nz_coder/runtime/prompt.py \
  nz_coder/tool_platform/permissioning/tool_groups.py \
  tests/test_plan_mode.py
python3 -m py_compile nz_coder/tools/plan_mode.py \
  nz_coder/state/sessions.py nz_coder/runtime/loop.py \
  nz_coder/runtime/subagent.py nz_coder/runtime/prompt.py \
  nz_coder/tool_platform/permissioning/tool_groups.py \
  tests/test_plan_mode.py
python3 -m pytest -q
```

### 13.7 学习重点

1. Plan mode 不是“模型先输出一段计划”，而是具有写权限边界和用户审批边界的运行时状态。
2. “计划文件可写”应做成能力很窄的专属工具，而不是把通用文件工具放开后依赖路径提示词约束。
3. 审批产生的权限变化必须考虑同一模型响应中的后续 tool call；批次后切换比 handler 内立即切换安全。
4. 用户评审的是某一份具体计划内容，因此审批前后要比较内容指纹，不能只相信旧 title/summary。
5. 交互工具必须有无头快速失败路径，模式切换服务也必须按 AgentLoop 会话作用域隔离。
6. 对齐源码时，目录名、父仓库提交或 `git -C` 的输出都不能替代真实版本来源；本项只陈述当前本地文件
   能直接证明的行为。

### 13.8 设计边界与剩余差距

当前接受的边界：

- 终端使用两次结构化选择代替 InfCode 的 GUI plan approval card；
- 没有 build/plan 两个独立 agent profile，复用同一个 AgentLoop 和 PermissionManager；
- 计划路径按 session ID 命名，没有 InfCode 的 slug、标题派生文件名或历史计划列表；
- title/summary 只进入工具结果和终端问题，没有独立结构化 session part 或客户端 metadata；
- InfCode 可在审批后直接按用户手工修改的最新版计划进入 Build；NZ-Coder 检测到变化后要求重新审批；
- Plan 期间普通源码写入由 PermissionManager 拒绝，但系统级只读判定仍取决于现有 Bash classifier；
- 手工执行 `/mode plan` 时没有可恢复的“上一模式”记录，`plan_exit` 默认回到 `default`；通过
  `plan_enter` 进入时会正确恢复原来的 `default`、`auto` 或 `acceptEdits`；
- 无交互 adapter 时需要审批的 `plan_enter`/`plan_exit` 会快速失败，目前没有异步 pending approval 或
  断线恢复；
- 本项没有把旧的自动 planning scratchpad 管线与显式 Plan mode 合并，两者用途不同：前者帮助模型内部
  分解任务，后者建立用户可评审的只读阶段。

后续如果产品入口需要，可继续：

- 在 GUI/HTTP 客户端实现异步 plan card、request ID、approve/reject 和断线恢复；
- 持久化 pending transition 与进入前 Build mode，使进程退出后仍可恢复审批状态；
- 为计划生成 slug、历史列表和 `/plan open` 等用户命令；
- 在 provider capability 中只向支持交互的客户端暴露 `plan_enter`/`plan_exit`；
- 增加 mode duration、拒绝原因、计划重写次数和 approval latency trace，但不记录敏感计划正文。

## 14. A012：普通修改的 patch 风险复核与保守重规划

### 14.1 InfCode 参考能力与准确边界

本项继续只以当前本地 InfCode 源码快照为证据，不绑定 Git 提交。主要参考：

- `infcode-dev/infcode-dev/packages/opencode/src/kilocode/review/review.ts`：从未提交或分支 diff 构造
  Local Review，要求只审查 diff 范围、先读取完整上下文，只报告达到置信度阈值的问题，并把审查保持为
  advisory；
- `infcode-dev/infcode-dev/packages/opencode/src/kilocode/review/worktree-diff.ts`：同时收集 tracked 与
  untracked 文件，保存 before/after、状态、增删行和 patch；
- `infcode-dev/infcode-dev/packages/opencode/src/session/processor.ts` 与 `message-v2.ts`：把一次编辑阶段的
  snapshot/patch 记录到 session part，并对大 patch 做传输裁剪；
- `infcode-dev/infcode-dev/packages/opencode/src/session/prompt/kimi.txt`：强调 feature 对现有代码保持
  minimal intrusions。

必须明确：当前本地 InfCode 没有一个通用运行时模块，会在“删除公开方法”后自动触发 replan。它提供的是
diff/snapshot 基础设施、手工 Local Review 和保守审查原则。A012 不是逐行移植某个 InfCode 类，而是把这些
原则接到 NZ-Coder 已有的 `impact_analyzer`、ChangeTracker、planning 和 reviewer 上。本文因此称它为
“对齐并增强”，不把 NZ-Coder 的自动风险反馈描述成 InfCode 原生行为。

### 14.2 NZ-Coder 原有不足

A012 前已经存在以下零件：

- `analyze_impact` 可按文件数量、敏感路径、diff 大小和测试变化生成风险摘要；
- RuntimeState 可保存 changed files、验证状态和自动 planning/replan 信息；
- Reviewer 可读取 impact review，非平凡 diff 没有风险摘要时给出 limitation；
- ChangeTracker 已保存工具写入前后的文件内容，支持 diff、undo 和 redo。

但这些能力没有形成普通编码任务的闭环：

1. `analyze_impact` 主要是模型手工调用，并不会在成功写入后自动运行；
2. 影响分析默认依赖 Git，嵌套目录、无仓库工作区或父仓库会让范围含义不可靠；
3. 分析器只能说 high/medium/low，不能指出“公开符号被删除”或“修改超出用户点名路径”这类需要改变
   实现方案的信号；
4. 自动 replan 只看停滞、复杂度升级和重复验证失败，不看当前 patch 自身风险；
5. Reviewer 不知道风险是否已经经过重规划，容易把“记录过 impact”误当成“风险已处理”；
6. ChangeTracker 的持久化 after 快照可能在外部回滚或文件再次变化后过期，风险分析若直接读取它会审查
   已不存在的改动。

### 14.3 实现结果

普通写工具成功后的核心调用链现在是：

```text
write_file / edit_file / apply_patch 等写工具全部成功
  -> TransactionManager commit
  -> ChangeTracker 用最初 before + 当前磁盘内容重建实时 diff、changed files 和 deleted files
  -> analyze_patch_impact(..., requested_paths, task_mode)
  -> 生成 risk level、risk signals 和 SHA-256 短指纹
  -> RuntimeState + RunEvidence 保存同一份报告
  -> 新的危险指纹只注入一次 <patch-risk-review>
  -> planning 已启用且已有计划时，下一轮触发一次 conservative replan
  -> Reviewer 判断该风险指纹是否已经完成 replan
```

当前会产生重规划信号的情况：

| 风险类别 | 判定 | 目的 |
|---|---|---|
| `deleted_public_symbols` | diff 净删除公开声明 | 防止为了局部修复意外删掉外部 API |
| `public_signature_change` | 同名公开声明的声明行发生变化 | 提醒确认兼容性和调用方影响 |
| `broad_scope_expansion` | 非项目创建任务修改超过 4 个源码文件 | 防止局部任务演变成大范围重构 |
| `requested_scope_expansion` | 源码修改超出用户明确点名的路径 | 要求重新确认这些额外文件确实必要 |

测试文件不参与 `requested_scope_expansion`，因为修改目标源码时补充相关测试通常是合理行为；
`project_creation` 也不因文件多或不匹配单一路径而触发范围 replan。风险等级仍可为 high，它表示验证和人工
复核成本较高，不等于实现一定错误。

公开声明检测当前只使用高置信度文本规则：Python 的非下划线 `class`/`def`、JavaScript/TypeScript 的
显式 `export` 声明以及 Rust 的 `pub` 声明。函数体内部改动、私有 Python helper 和单纯新增公开声明不会被
误报成删除或签名变化。

### 14.4 关键设计决策

#### 自动闭环以 ChangeTracker 为主，Git 只作为手工工具的 fallback

AgentLoop 审查的是“本轮由 NZ-Coder 文件工具产生、事务已经提交、当前磁盘仍存在”的变化。因此新增
`current_changed_paths()` 与 `render_current_diff()`：它们保留第一次记录的 before，每次读取当前文件作为
after。若文件已经恢复原状，当前 changed files 会变为空，即使旧的持久化 after 仍是修改后内容。

`analyze_patch_impact(changed_files=[])` 现在把显式空列表理解为“调用方确认当前没有变化”，不会再自动向
Git 回退。这一点能避免在嵌套项目里意外审查父仓库差异。只有独立手工调用 `analyze_impact` 且没有
ChangeTracker 变化时，才保留原有 Git fallback，便于审查用户已有工作树。

验证计划器同样区分“调用方已提供 changed files”和“需要自行发现”：自动路径把 ChangeTracker 的
`deleted_files` 一并显式传入，因此删除文件不会收到无效的逐文件编译命令，也不会为了识别删除状态再次调用
Git。只有 `changed_files=None` 的独立发现模式才读取 Git changed/deleted files。

#### 风险信号按 patch 指纹去重，不永久冻结符号

指纹由排序后的 changed files 与当前 diff 计算。相同 patch 多次刷新只注入一次反馈；patch 内容改变后产生
新指纹，才允许重新提示。这里没有实现“某个函数一旦通过测试就永远不能修改”的 frozen-symbol 规则，因为
后续正确修复可能确实需要改它。

#### replan 是一次保守复核，不是不可绕过的硬阻断

当 `PLANNING_ENABLED=1` 且已有计划时，新的危险指纹接入现有 `_should_replan()`。重规划 prompt 会带上完整
风险报告，要求删除偶然的 API/越界修改，除非用户任务明确需要。成功生成新计划后记录
`risk_replan_fingerprint`，同一风险不再重复消耗 replan 次数。项目画像也在每个 AgentLoop 中只扫描一次，
prompt 摘要与后续风险验证计划共享同一份结构化 profile，避免每个写批次重复遍历仓库。

如果 planning 未启用或当前没有计划，主循环仍会通过 `<patch-risk-review>` 要求模型重新读取声明和范围；
Reviewer 最终给出 `approved_with_limitations`，而不是永久禁止完成。这样默认交互不会平白增加一次独立模型
调用，同时也不会把尚未显式处理的危险 patch 伪装成完全 approved。

#### 不复制 SWE-bench 专用质量门

A012 没有把“新增/删除方法数量”“裸 except”等 SWE-bench retry 规则接到普通 AgentLoop，也没有运行官方
评测。普通任务只使用跨项目可解释、置信度较高的 API 与范围信号；测试是否需要升级仍由 A009 的分层验证
管线决定。

### 14.5 关键文件

- `nz_coder/state/changes.py`：按当前磁盘刷新 after 快照，生成无 Git 的当前 changed paths 和 unified diff；
- `nz_coder/intelligence/impact_analyzer.py`：公开声明解析、四类风险信号、指纹和格式化报告；
- `nz_coder/intelligence/verification_planner.py`：接收显式 deleted files；显式 changed files 模式不查询 Git；
- `nz_coder/runtime/runtime_state.py`：保存 patch risk、反馈指纹、replan 指纹，并把待复核信号写入运行时提示；
- `nz_coder/runtime/loop.py`：事务提交后刷新风险、一次性注入反馈，并把新风险接到既有 replan；
- `nz_coder/run_evidence.py`：保存结构化 impact report，并兼容解析手工工具的 replan/fingerprint 输出；
- `nz_coder/reviewer.py`：区分“存在风险摘要”和“该风险已经重规划”；
- `tests/test_impact_analyzer.py`：公开删除、签名变化、函数体修改、范围、项目创建、空列表语义和旧位置参数兼容；
- `tests/test_changes_undo.py`：磁盘恢复后当前 diff 消失、旧 after 不污染风险判断；
- `tests/test_loop_fake.py`：无 Git 自动刷新、同指纹一次反馈、风险触发一次 replan；
- `tests/test_verification_planner.py`：显式 changed/deleted paths 不触发 Git，删除文件不生成编译命令；
- `tests/test_reviewer.py`、`tests/test_run_evidence.py`：review limitation 与报告元数据解析。

### 14.6 验证结果

完成本项时：

- 影响分析、ChangeTracker、验证计划、Fake Loop、Reviewer 与 RunEvidence 聚焦回归：`156 passed`；
- 完整测试套件：`533 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- `impact_analyzer.py`、verification planner、changes/runtime state/evidence/reviewer 和相关聚焦测试严格 Ruff：
  `All checks passed!`；
- 包含历史导入布局的 `runtime/loop.py` 与 `test_loop_fake.py` 使用项目既有兼容忽略项
  `F401,E402,F811` 后 Ruff：`All checks passed!`；
- A012 七个修改源码文件 `py_compile` 通过；
- 新增的 `deleted_files` 均放在公开函数参数末尾，旧位置参数顺序由测试锁定，避免静默接口破坏；
- fake AgentLoop 在没有 `.git` 的临时 workspace 中删除公开函数，确认风险报告、一次性反馈与 evidence 同步；
- 该无 Git 测试把 verification planner 的 `_git_deleted_files()` 替换为立即抛错，确认自动闭环没有隐藏的
  Git 删除探测；
- 恢复原文件后再次刷新，确认 `affected_files=[]`、`requires_replan=false`、`has_diff=false`；
- 没有发送真实模型请求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q \
  tests/test_impact_analyzer.py tests/test_changes_undo.py \
  tests/test_loop_fake.py tests/test_reviewer.py tests/test_run_evidence.py \
  tests/test_verification_planner.py
python3 -m ruff check \
  nz_coder/state/changes.py nz_coder/intelligence/impact_analyzer.py \
  nz_coder/intelligence/verification_planner.py \
  nz_coder/runtime/runtime_state.py nz_coder/run_evidence.py nz_coder/reviewer.py \
  tests/test_impact_analyzer.py tests/test_changes_undo.py \
  tests/test_reviewer.py tests/test_run_evidence.py tests/test_verification_planner.py
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/runtime/loop.py tests/test_loop_fake.py
python3 -m py_compile \
  nz_coder/state/changes.py nz_coder/intelligence/impact_analyzer.py \
  nz_coder/intelligence/verification_planner.py \
  nz_coder/runtime/runtime_state.py nz_coder/run_evidence.py \
  nz_coder/reviewer.py nz_coder/runtime/loop.py
python3 -m pytest -q
```

### 14.7 学习重点

1. “风险高”与“方案必须改变”不是同一概念；可解释的 risk signal 比单一 high/medium/low 更适合触发动作。
2. Agent 自己的修改应从事务/ChangeTracker 真相读取，不能默认把工作树全部变化都归因给当前 Agent。
3. 保存的 after 只是历史证据；做完成前判断时要与当前磁盘重新对照，否则 rollback/undo 后仍可能审查旧 patch。
4. `None` 与空列表有不同语义：前者表示“请自行发现”，后者表示“调用方已确认没有项”，不能用 `or`
   把两者合并。
5. 重规划触发必须去重并受现有 attempt 上限约束，否则保守机制本身会制造循环。
6. 对齐一个产品原则时，应明确哪些行为来自参考项目，哪些是根据本项目架构做的增强。

### 14.8 设计边界与剩余差距

当前接受的边界：

- 公开声明识别是保守文本规则，不是完整 AST/LSP API compatibility 分析；Java/Kotlin/C# 等语言目前只能
  获得范围和 diff 大小风险，不能识别公开符号删除；
- Python 规则会识别类中的公开方法，但不能判断装饰器、重载、动态导出或 `__all__` 的真实公共性；
- 自动刷新只覆盖接入 ChangeTracker 的文件工具；Bash、外部进程或用户手工产生的变化不会被自动归因给
  当前 Agent，手工 `analyze_impact` 才会使用 Git worktree fallback；
- 用户任务中的 requested paths 来自现有轻量任务分析，若用户只用自然语言描述模块而没有明确路径，
  `requested_scope_expansion` 不会触发；
- planning 关闭时不额外调用独立 replan 模型，只依赖主循环反馈和 Reviewer limitation；
- 风险复核不证明行为正确，仍需 A009 的静态检查、目标测试和必要回归；
- 当前没有 InfCode Local Review 那样的独立审查命令、置信度表格和“选择修复哪些问题”的交互 UI；
- 没有调用图或 LSP references 影响面分析，也没有自动把被改签名的调用方加入验证计划。

后续可考虑把 A001 LSP references 与 A002 Repo Map 用于公开签名变更的调用方确认，但应保持查询失败时
可降级，不能让可选语言服务器成为写入成功的硬依赖。

## 15. A013：调度与恢复可观测性

### 15.1 InfCode 参考能力与准确边界

本项仍然只以当前本地 InfCode 源码快照为证据，不绑定 Git 提交。主要参考：

- `infcode-dev/infcode-dev/packages/opencode/src/session/message-v2.ts`：工具状态从 `pending` 进入
  `running` 时保存 `time.start`，进入 `completed` 或 `error` 时保存 `time.end`；
- `infcode-dev/infcode-dev/packages/opencode/src/session/processor.ts`：成功、失败和中断三条路径均用
  start/end 计算 `durationMs`，并分别调用 complete/fail/abort 指标接口；
- `infcode-dev/infcode-dev/packages/opencode/src/infcode/metrics/instrument/tool.ts`：把工具指标归类为
  success、error、timeout 或 cancelled，指标异常由内部 `try/catch` 吞掉，不影响 Agent 主流程；
- `infcode-dev/infcode-dev/packages/opencode/src/session/processor.ts` 的 doom-loop 检查：读取最近固定数量的
  tool parts，比较工具名和输入是否连续相同，再进入权限确认。

当前本地 InfCode 能直接证明的是“每项工具有生命周期时间和结果指标”。没有看到与 NZ-Coder A007 调度器
结构一一对应的 `parallel_read` segment、serial barrier drain wait 或 streak reset 计数模块。因此 A013 的
per-call duration 是直接能力对齐；batch/segment/barrier/streak-reset 是为了让 NZ-Coder 自研调度和恢复机制
可解释而增加的本地增强，本文不把它们描述成 InfCode 原生事件。

### 15.2 NZ-Coder 原有不足

A013 前已经有两项关键基础：

- A007 用 `read`、`serial`、`write` execution mode 把连续只读段并行执行，并以状态/写工具作为顺序屏障；
- A008 对同名同参连续调用计数，达到阈值时在 dispatch 前阻断，工具或参数改变时重置 streak。

但原有 JSONL trace 只能看到：

```text
tool_call: name + status + output_len + output
```

它无法回答实际调试最常见的问题：

1. 慢的是哪个工具，权限等待和工具执行总共用了多久；
2. 一个模型响应中的只读工具是否真的并行，实际峰值是多少；
3. serial/write 屏障在开始前等了多久才让上一段只读任务全部 drain；
4. 并发上限不足时，后续只读调用在 scheduler 中排队了多久；
5. doom-loop streak 是被哪个不同工具或哪组新参数打断的；
6. 一次 run 总共有多少 batch、parallel/serial segment、屏障等待和 streak reset；
7. `summarize_trace()` 只能汇总工具次数，不能给出耗时与调度结论。

这意味着 A007/A008 虽有正确性测试，但真实运行变慢或恢复策略频繁重置时，只能重新读完整 trace 和猜测。

### 15.3 实现结果

核心调用链现在是：

```text
LLM 返回 tool calls
  -> AgentLoop 分配 batch-N，记录 tool_batch_started
  -> RecoveryState 比较 canonical tool signature
     -> 工具或参数变化时生成 doom_loop_streak_reset
  -> scheduler 按连续 read / serial barrier 划分 segment
     -> _ConcurrentProbe 记录 worker 实际并发峰值和 queue wait
     -> serial barrier 记录前一 parallel segment 的 drain duration
  -> ToolExecutor 用 perf_counter 包住解析、权限和 dispatch
  -> ToolExecutionResult 携带 duration_ms / queue_wait_ms
  -> tool_schedule_segment + tool_batch_completed
  -> tool_call 携带 call id、序号、分类和耗时
  -> runtime.tool_observability 保存本 run 聚合值
  -> summarize_trace 输出耗时、peak、barrier wait 和 reset 数
```

新增/扩展的 trace 事件如下：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `tool_batch_started` | `batch_id`、call names/count、`has_write`、parallel limit | 记录调度输入，而不是事后猜测 |
| `tool_schedule_segment` | kind、names/count、duration、peak、queue wait、barrier wait | 解释 read segment 与 serial barrier 的实际时序 |
| `tool_batch_completed` | mode、wall/total-call time、peak、segment counts、barrier wait | 给出一次模型工具批次的总体表现 |
| `tool_call` | call id/index、status 分类、executed/write、duration/queue wait | 定位具体慢调用并区分拒绝、dispatch error 和 command nonzero |
| `doom_loop_streak_reset` | reason、previous/next tool、previous count、reset count | 解释为什么相同调用计数回到 1 |

最终 `result["runtime"]["tool_observability"]` 包含：

- batch 和 call 数；
- batch wall time 与所有 tool duration 总和；
- run 内实际 peak concurrency；
- parallel/non-parallel segment 数（`serial_segments` 同时包含 `serial_barrier` 和
  `sequential_guarded` 汇总段）；
- barrier drain wait 总量；
- streak reset 总数。

### 15.4 指标语义与关键设计决策

#### duration 与 queue wait 分开

`duration_ms` 使用单调的 `time.perf_counter()`，覆盖参数解析、权限判断/用户确认和真实 dispatch；它不使用
墙上时间，因此系统时间调整不会产生负数。`queue_wait_ms` 从 parallel read segment 创建开始计算，到调用真正
获得 worker 并开始执行为止。并发数超过 `MAX_PARALLEL_TASKS` 时，后续调用的 queue wait 会包含等待前一批
worker 释放的时间。

这两个数字不能相加后再与 batch wall time直接比较：并行调用的 duration 会重叠，total tool duration 大于
wall time 是正常现象。

#### barrier wait 是 drain time，不是锁争用时间

当 segment 形如 `read, read -> serial` 时，serial barrier 只能在前一 parallel segment 全部完成后开始。
A013 把该 parallel segment 从启动到 drain 的 duration 记为这个 barrier 的 `barrier_wait_ms`。它表示“屏障
开始前被前置只读工作推迟了多久”，不是 mutex acquire 时间，也不表示 serial 工具自身耗时。

如果批次没有前置 parallel segment，barrier wait 为 0；最后一个 parallel segment 后没有 barrier 时，也不会
虚构 barrier wait。

#### 实际 peak，而不是配置上限

`parallel_limit` 只记录配置；`peak_concurrency` 由 `_ConcurrentProbe` 在 worker 进入/退出时用锁保护地计数。
两个极快任务可能即使配置上限为 4，实际 peak 仍只有 1。这比把 `min(call_count, limit)` 当成真实并发更可信。

#### 可观测性失败不能改变工具结果

调度器的 `on_segment`/`on_metrics` 是新增在参数末尾的可选 callback，旧的三个位置参数和返回列表保持不变；
callback 抛异常会被吞掉。`ToolExecutionResult` 只在 dataclass 末尾增加带默认值的字段，原来的七个位置参数构造
仍然有效。TraceRecorder 的目录创建或 JSONL append 遇到 `OSError` 时会关闭/丢弃本地事件并更新
`dropped_events`、`last_write_error`，不会让指标成为事务提交或工具成功的前置条件；Dodo 的远程队列仍保持
原有 fire-and-forget 行为。

#### streak reset 不记录输入正文

RecoveryState 继续用 canonical JSON 比较输入，但 reset 事件只保存 reason、工具名和连续次数，不保存完整
arguments，避免把命令、路径或可能的敏感参数再复制一份到 trace。reason 区分：

- `tool_changed`：下一项工具名不同；
- `arguments_changed`：工具相同但 canonical arguments 不同；
- `manual`：调用方显式清空历史；
- `guard_disabled`：阈值非正、guard 被关闭时结束旧 streak。

每次新的 `run()` 使用 `start_tool_call_run()` 清空前一 run 的 signature 和计数，不把跨任务边界当成本 run 的
reset 事件。

### 15.5 关键文件

- `nz_coder/runtime/tool_executor.py`：在不改变结果分类的情况下测量每项调用 duration；
- `nz_coder/runtime/recovery.py`：生成并消费结构化 streak reset 事件，维护 per-run reset count；
- `nz_coder/runtime/loop.py`：batch id、segment probe、同步/异步调度观测、trace 事件与 runtime 聚合；
- `nz_coder/state/trace.py`：在 trace 摘要中展示 total/avg/max tool duration、batch peak、barrier wait 和 reset；
- `tests/test_observability.py`：专门覆盖 timing、同步/异步 segment、barrier、reset、真实 JSONL 和摘要；
- `tests/test_recovery.py`：确认原 streak 计数与 reset 行为兼容；
- `tests/test_runtime_context.py`：确认同步/异步屏障时序和只读并发规则未改变；
- `tests/test_loop_fake.py`：确认完整 AgentLoop、事务、恢复和 trace 路径未回归。

### 15.6 验证结果

完成本项时：

- Observability、Recovery、Scheduler 和 Fake Loop 聚焦回归：`61 passed`；
- 完整测试套件：`538 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing 告警；
- `tool_executor.py`、`recovery.py`、`trace.py`、新测试和相关聚焦测试严格 Ruff：
  `All checks passed!`；
- 包含历史导入布局的 `runtime/loop.py` 与 `test_loop_fake.py` 使用项目既有兼容忽略项
  `F401,E402,F811` 后 Ruff：`All checks passed!`；
- A013 四个修改源码文件与新测试 `py_compile` 通过；
- fake AgentLoop 真实执行两个约 10ms 的 read probe 加一个 serial probe，JSONL 显示 actual peak=2、
  barrier wait>0、三个带 call id/duration 的 tool events，并在 runtime 与 summary 中得到相同聚合；
- 同步和异步 scheduler 都验证 `parallel_read -> serial_barrier -> parallel_read` 的 segment 顺序；
- 故障注入让目标 trace path 的 `Path.open()` 抛出 `OSError`，确认 Agent 不抛异常且 dropped event/最后错误可查；
- 没有发送真实模型请求，没有上传外部 telemetry，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q \
  tests/test_observability.py tests/test_recovery.py \
  tests/test_runtime_context.py tests/test_loop_fake.py
python3 -m ruff check \
  nz_coder/runtime/tool_executor.py nz_coder/runtime/recovery.py \
  nz_coder/state/trace.py tests/test_observability.py \
  tests/test_recovery.py tests/test_runtime_context.py
python3 -m ruff check --ignore F401,E402,F811 \
  nz_coder/runtime/loop.py tests/test_loop_fake.py
python3 -m py_compile \
  nz_coder/runtime/tool_executor.py nz_coder/runtime/recovery.py \
  nz_coder/runtime/loop.py nz_coder/state/trace.py \
  tests/test_observability.py
python3 -m pytest -q
```

### 15.7 学习重点

1. “配置允许并发”不是“运行时真的并发”；需要 worker enter/exit 计数才能证明 actual peak。
2. 工具执行耗时、scheduler queue wait、barrier drain wait 和 batch wall time是四种不同指标，不能混用。
3. 并行任务的 duration 会重叠，因此 `sum(tool duration) > batch wall` 可以是正常且有价值的并行证据。
4. 可观测 callback 必须在执行契约之外；埋点失败不能回滚文件、改变 tool result 或中断 Agent。
5. reset 次数本身不够，必须记录 reset reason 和 previous count，才能区分有效探索与无意义参数抖动。
6. 给现有 API 增加观测字段时，放在末尾并提供默认值，才能避免静默破坏旧位置参数调用。

### 15.8 设计边界与剩余差距

当前接受的边界：

- NZ-Coder 把 timing 存在本地 JSONL trace 和最终 runtime summary，没有 InfCode 那样的 metrics backend、
  URI 聚合或跨 run dashboard；
- 没有 histogram、p50/p95/p99、按模型/项目/工具版本聚合，也没有长期性能回归告警；
- 工具被 doom-loop guard 在 dispatch 前拒绝时 duration 为 0，`executed=false` 用来区分“没有执行”与“执行极快”；
- KeyboardInterrupt 或进程被强杀发生在工具中间时，当前不保证生成 cancelled tool event；InfCode 有明确 abort
  state 与 cancelled metric；
- pre-tool hooks 存在或批次含 blocked call 时会进入 `sequential_guarded`，生成一个 wall/peak=1 的汇总
  segment，不再按原始 read/serial 边界细分，也不会声称原本可并行的调用实际并行；
- queue wait 是 scheduler 视角的延迟，包含显式分批等待，不是操作系统线程池内部 wait 的纯测量；
- barrier wait 只统计“parallel segment drain 后才能开始 serial barrier”，不统计 serial barrier 自身让后续
  segment 等待的时间；后者可直接从该 serial segment 的 duration 读取；
- 工具 start/end 没有作为独立 session message part 持久化，trace row 的 `ts` 是结果进入 trace 的时间，
  精确生命周期依赖 `duration_ms` 而不是两条 start/end event；
- 本项没有新增外部依赖、网络上报或用户输入正文采集。

后续如果要继续增强，可先实现本地 trace 聚合命令，按 tool name 输出 count/error rate/p50/p95；只有确认隐私、
采样和用户开关后，才考虑远程 telemetry。

## 16. A014：实例级运行时上下文隔离

### 16.1 InfCode 参考能力与准确边界

本项重新阅读当前本地 InfCode 源码快照，主要参考：

- `packages/opencode/src/effect/instance-state.ts`：`InstanceState.context` 从当前 Effect/Fiber 的
  `InstanceRef` 读取 instance，`directory` 与 `workspaceID` 从当前上下文派生；实例状态缓存按 directory
  取值，不由调用方临时覆盖进程级目录；
- 同文件的 `InstanceState.bind()`：捕获当前 instance，并在普通异步 callback 执行时恢复，避免 callback
  离开原 Effect scope 后落到错误 workspace；
- `packages/opencode/src/server/routes/instance/httpapi/middleware/instance-context.ts`：HTTP 路由根据请求中的
  directory/workspace 建立 instance scope，再在 scope 中运行 handler；
- `packages/opencode/src/session/network.ts`：延迟 `setTimeout` callback 使用 `InstanceState.bind()`，源码注释
  明确说明多个 directory 并发等待时如果不恢复 context，reply 可能落到错误 instance。

InfCode 使用 Effect、LocalContext、Fiber reference 和 instance store；NZ-Coder 没有这些框架。本项对齐的是
“运行状态随当前执行链传播、并发执行互不覆盖、离开 scope 自动恢复”的语义，不复制 Effect API，也不声称
NZ-Coder 已具有 InfCode 完整的 instance lifecycle/disposer/store。

### 16.2 NZ-Coder 原有状态与剩余问题

A014 前，主运行链已经不是最初审计表描述的纯全局状态：

- `runtime/workdir.py` 已用 `ContextVar` 保存 workspace override；
- `tools/files.py` 的 TransactionManager 与 ChangeTracker 也已用 `ContextVar` 绑定；
- session、memory、skills、question、plan controller 和 parent-agent metadata 均已有 context-local binding；
- `tests/test_runtime_context.py` 已验证两个线程/两个 Agent 的 workspace、事务和 ChangeTracker 不串线。

但生产代码仍有 26 条直接配置属性赋值，覆盖五类运行期状态：

| 状态 | 原写入位置 | 并发风险 |
|---|---|---|
| workspace | Dodo dev、SWE orchestrator、local/Aider benchmark、eval runner | 一个入口切换仓库时，另一个 Agent 可能读取同一 `config.WORKDIR` |
| max agent turns | Dodo、eval runner、SWE CLI | 一个任务的轮次预算临时改变其他任务 |
| agent timeout | SWE orchestrator 动态创建/删除 config 属性 | 同进程 attempt 可能读到另一实例的总超时 |
| max parallel tasks | offline parallel benchmark | benchmark 运行期间改变真实 Agent 的 scheduler 上限 |
| broad-test guard | AgentLoop 在有 diff 后写 `config.BLOCK_BROAD_TESTS` | 任一 Agent 写文件后可能禁止其他 workspace 的 broad test |

其中 broad-test guard 最危险：它不是静态配置，而是每个 run 内从 `false` 变成 `true` 的状态机。把它放在
`config` 上，会让并发 Agent 的验证决策互相污染。旧实现还依赖 `_init_run()` 动态创建该属性，测试能否访问
它取决于此前是否运行过 Agent，初始化顺序也不稳定。

### 16.3 实现结果

现在的绑定链路是：

```text
环境变量 / config.py
  -> 只作为进程默认值，不在任务执行时改写

入口为一个任务选择 workspace / turns / timeout / parallel limit
  -> scoped_workdir(...)
  -> scoped_runtime_overrides(...)
  -> 构造并运行 AgentLoop

AgentLoop.run()
  -> 绑定自己的 workspace/session/tool state/memory/skills/UI/parent context
  -> 新建 scoped_broad_test_guard(false)
  -> 写工具成功且 RuntimeState.has_diff=true
     -> 仅把当前 execution 的 broad-test guard 设为 true
  -> Bash 只读取当前 execution guard
  -> run 返回或抛错时 ContextVar token 自动恢复
```

`runtime/execution_context.py` 新增三项只读 override 和一项动态 guard：

| 接口 | 默认值来源 | 运行时用途 |
|---|---|---|
| `max_agent_turns()` | `config.MAX_AGENT_TURNS` | `_init_run()` 轮次上限和用户 turn hint cap |
| `agent_timeout_seconds()` | 可选 `config.AGENT_TIMEOUT_SECONDS`，否则 0 | RuntimeState 总运行超时 |
| `max_parallel_tasks()` | `config.MAX_PARALLEL_TASKS` | batch trace、同步和异步 scheduler 限流 |
| `broad_tests_blocked()` | 每个 scope 初始 `false` | 有 source diff 后阻止当前 Agent 的 broad runner |

`scoped_runtime_overrides()` 支持嵌套：内层只覆盖显式传入字段，其他字段继承外层；退出内层后恢复外层，退出
最外层后继续使用环境配置默认值。传入的轮次、超时和并发值在 scope 内规范化，不修改 config object。

#### 主 Agent 与子 Agent

`AgentLoop.run()` 每次建立新的 broad-test scope，即使同一个 Python task 外层已有 guard，也不会继承另一个 run
的 diff 状态。`_init_run()` 显式把当前 scope 复位，写入观察只更新当前 ContextVar。

手写子 Agent loop 没有复用 `AgentLoop.run()`，因此单独建立自己的 broad-test scope。general-purpose 子 Agent
写工具成功后只阻断该 child 后续的 broad test；父 Agent和并行 sibling 的状态不变。只读 explore/plan/
reflection child 不会因为父 Agent 已有 diff 而错误继承阻断状态。

#### 调度线程与 SWE fork

同步 scheduler 原本已用 `copy_context().run(...)` 提交 worker，`asyncio.to_thread()` 也会复制当前 context，
因此 parallel limit、workspace 和 tool bindings 能进入只读 worker。本项没有宣称 ContextVar 会自动进入任意
用户创建的裸线程；普通线程入口仍应在 worker 内显式建立 scope或使用 `copy_context()`。

SWE-bench timeout attempt 在 POSIX 使用 `multiprocessing.get_context("fork")`。本项在 `process.start()` 前绑定
workspace、timeout 和外层默认轮次，fork child 继承当时 context，再在 child 中构造 AgentLoop。专项测试让真实
fork child 返回四项值，确认不是只验证父进程。

#### 入口迁移

以下入口不再保存/覆盖/恢复 config 属性：

- Dodo dev worker 的 `_temporary_workdir()`；
- local evaluation runner 的 `_temporary_workdir()`；
- offline parallel benchmark；
- Aider Python exercise helper；
- 内置 coding-task benchmark；
- SWE-bench orchestrator 与 run/retry CLI。

迁移保留原函数参数、返回类型、CLI 选项和环境变量默认行为。SWE CLI 在用户没有设置
`MAX_AGENT_TURNS` 时仍使用 80，但该值只存在于本次 batch 的 runtime scope。

### 16.4 关键设计决策

#### config 是默认值，不是任务状态仓库

`config.py` 仍负责读取环境变量，例如默认模型、默认轮次和默认并发数。删除它没有意义；真正需要消除的是
运行中为了一个任务写回模块属性。A014 后生产包 AST 扫描确认没有 `config.X = ...` 或 `_config.X = ...`。

#### 不把全部配置复制进 AgentLoop 构造函数

给 AgentLoop 增加 workspace、turns、timeout、parallel limit 等大量参数会把入口配置、子 Agent、scheduler
helper 和 benchmark helper 全部耦合到一个构造函数。ContextVar 让深层 Bash/scheduler helper 读取当前
execution 值，同时保持现有构造接口和测试 fake 兼容。

#### broad-test guard 与静态 override 分离

turns、timeout 和 parallel limit 在一个 execution 内是只读配置；broad-test guard 则会随成功写入改变。
将两者放进同一个可变字典容易让嵌套 scope共享引用。当前实现使用 immutable `RuntimeOverrides` 加独立 bool
ContextVar，避免 aliasing，也让“设置 guard”不会意外覆盖其他参数。

#### 异常恢复依赖 token，而不是手写 finally 复制旧值

旧入口各自保存 `old_workdir`，并在多个 return/except/finally 分支恢复；漏掉一个分支就污染进程。
ContextVar context manager 使用 token 恢复嵌套前状态，异常路径和正常路径使用同一退出机制。内置 benchmark
因此删除了多个提前 return 前的重复恢复语句。

### 16.5 关键文件

- `nz_coder/runtime/execution_context.py`：immutable runtime overrides、getter、嵌套 scope 和 broad-test guard；
- `nz_coder/runtime/loop.py`：每次 run 建立 guard，读取当前轮次/超时/并发并在成功写入后只更新本 scope；
- `nz_coder/tools/bash.py`：broad runner 只读取当前 execution guard；
- `nz_coder/runtime/subagent.py`：child 独立 guard 与成功写入后的局部状态转换；
- `nz_coder/runtime/workdir.py`、`nz_coder/tools/files.py`：复用既有 workspace/事务/ChangeTracker ContextVar；
- `nz_coder/dodo/dev_headless.py`：Dodo dev 的 workspace/turns scope；
- `nz_coder/evaluation/*.py`：local/Aider/parallel/内置 benchmark 迁移；
- `nz_coder/swebench/orchestrator.py`、`nz_coder/swebench/cli.py`：workspace、timeout、默认轮次与 fork 传播；
- `tests/test_execution_context.py`：嵌套恢复、双线程、Bash guard、evaluation scope、fork 和静态配置写入防线；
- `tests/test_runtime_context.py`：双 Agent workspace/tool state 与 read-only child 并发回归。

### 16.6 验证结果

完成本项时：

- Context、Loop、Dodo、SWE-bench 和 Scheduler 聚焦回归：`101 passed`，只有 2 个既有 websockets/uvicorn 告警；
- 完整测试套件：`544 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 新 `execution_context.py` 与专项测试严格 Ruff：`All checks passed!`；
- 历史入口文件使用兼容忽略项 `F401,F541,F821,E402,F811` 后 Ruff：`All checks passed!`；这些是已有文件的
  导入布局、未解析前向注解和旧语法告警，本项没有用自动 fix 扩大修改范围；
- A014 修改源码与新测试 `py_compile` 通过；
- AST 扫描所有 `nz_coder/**/*.py`，生产代码的 `config.X = ...` / `_config.X = ...` 数量为 0；
- 两个真实线程同时绑定不同 workspace、turns、timeout、parallel limit 和 guard，结果完全隔离；
- Bash 故障注入确认 blocked scope 不启动 subprocess，unblocked sibling scope 正常执行；
- POSIX SWE fork child 实际读取到父 attempt 绑定的 workspace、17 turns、4.5s timeout 和 parallel limit 3；
- 没有发送真实模型请求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q \
  tests/test_execution_context.py tests/test_runtime_context.py \
  tests/test_loop_fake.py tests/test_parallel_benchmark.py \
  tests/test_swebench_lite.py tests/test_dodo_dev_vertical_slice.py
python3 -m ruff check \
  nz_coder/runtime/execution_context.py tests/test_execution_context.py
python3 -m py_compile \
  nz_coder/runtime/execution_context.py nz_coder/runtime/loop.py \
  nz_coder/runtime/subagent.py nz_coder/tools/bash.py \
  nz_coder/dodo/dev_headless.py nz_coder/evaluation/eval_runner.py \
  nz_coder/evaluation/parallel_benchmark.py \
  nz_coder/evaluation/aider_benchmark.py nz_coder/evaluation/benchmark.py \
  nz_coder/swebench/orchestrator.py nz_coder/swebench/cli.py \
  tests/test_execution_context.py
python3 -m pytest -q
```

### 16.7 学习重点

1. 环境配置可以是进程级默认值，但“当前任务已经写过 source diff”一定是实例状态。
2. 手工保存并恢复全局变量只能保证单线程 happy path；嵌套 scope 和异常分支需要 token 化恢复。
3. ContextVar 解决的是 execution context 传播，不等于任意线程自动继承；线程池、`to_thread` 和 fork 要分别验证。
4. 子 Agent 若手写自己的 loop，就必须显式复用主 Loop 的隔离约束，不能假设 workspace 隔离会自动隔离所有 gate。
5. 静态 AST 防线能把“目前没有全局写入”变成持续可测试的工程约束。
6. 兼容迁移应保留入口参数和默认行为，让调用方无需理解 ContextVar 才能继续使用。

### 16.8 设计边界与剩余差距

当前接受的边界：

- 工具注册表、optional pack 加载状态、部分 LSP/client cache 仍是进程级共享设施；它们按设计跨 Agent 复用，
  但如果未来允许运行时动态卸载或覆盖工具，需要另做 registry snapshot/locking；
- `config.py` 的其他常量仍可被外部 Python 调用方直接赋值；A014 保证 NZ-Coder 生产代码自己不这样做，无法阻止
  第三方脚本绕过公开 scope；
- POSIX fork context 传播已验证；Windows/macOS spawn 不会继承 Python ContextVar，当前 SWE timeout 路径也只在
  `hasattr(os, "fork")` 时启用子进程，因此没有伪称跨平台 spawn 已对齐；
- 任意裸 `threading.Thread` 不自动继承 ContextVar；内部 scheduler 已显式复制，外部扩展需要使用
  `copy_context()` 或在 worker 内建立 scope；
- `RuntimeOverrides` 当前只覆盖真正存在临时写入的 turns/timeout/parallel limit，没有把所有静态 config 常量
  复制成 per-Agent capability snapshot；
- general-purpose 写子 Agent 仍被主 scheduler 串行调度。本项消除了 workspace/gate 串线前置风险，但没有实现
  多写 Agent 的后台 job manager、取消、join 和安全合并；
- Dodo task runner 本身每任务使用独立 subprocess；本项主要保证 dev worker 内部和同进程 helper 不依赖全局切换；
- 没有实现 InfCode 的 instance disposer、按 directory 的 scoped service cache 或完整 HTTP instance store。

A014 完成时建议下一步进入 provider capability registry：把模型窗口、原生 tool/reasoning 能力、system prompt
family 和请求参数策略从单一全局配置拆成可测试的模型能力描述。该建议已由下面的 A015 第一阶段落实。

## 17. A015：Provider capability registry

### 17.1 InfCode 参考能力与准确边界

本项重新阅读当前本地 InfCode 源码快照，主要参考：

- `packages/opencode/src/provider/provider.ts`：`Provider.Model` 把 provider/model id、API adapter、family、
  context/input/output limit、temperature/reasoning/attachment/toolcall、输入输出 modality、interleaved reasoning、
  options 和 headers 组织为统一模型记录；`fromModelsDevModel()` 把 models.dev 元数据转换成运行时模型；
- `packages/opencode/src/session/system.ts`：根据显式 `model.prompt` 或模型 API id 选择 Anthropic、Gemini、GPT、
  Codex、Kimi 等不同 system prompt；
- `packages/opencode/src/provider/transform.ts`：根据 provider、adapter 和 model capability 选择 temperature、top-p、
  reasoning 参数、provider options 与每轮最大输出，并对 GPT-5/OpenAI-compatible 等特殊组合做请求兼容；
- `packages/opencode/src/session/llm.ts`：只有模型声明支持 temperature 时才发送 temperature，并把模型 limit 与
  capability 传入请求和观测链路；
- `packages/opencode/src/session/context-budget.ts`：上下文预算从当前模型声明的 context/output limit 派生，而不是
  所有模型共用一个固定窗口。

InfCode 的 registry 连接 models.dev 快照、动态 provider discovery、插件、自定义 loader、凭据与大量 AI SDK adapter。
NZ-Coder 仍坚持只使用标准库和已有客户端，也没有动态下载模型目录。本项对齐的是“先解析模型能力记录，再由提示、
预算和请求层消费”的核心结构，不声称复制了 InfCode 的 provider 生态或完整模型数据库。

### 17.2 NZ-Coder 原有状态与实际不足

A015 前，NZ-Coder 已经比最初差距表中的“固定 OpenAI-compatible 客户端”更进一步：

- `providers/openai_compatible.py` 支持 OpenAI Chat Completions 兼容端点；
- `providers/anthropic.py` 已实现原生 Messages API 的消息、工具、stream 与 reasoning 归一化；
- `providers/gemini.py` 已实现原生 generateContent、function call、stream 与 thought signature 往返；
- `providers/normalized.py` 为三种 adapter 提供统一的 OpenAI-shaped response；
- `evaluation/provider_smoke.py` 已有可选的 text/tool/stream 真实冒烟流程。

因此 A015 没有重复创建 adapter。剩余问题位于 adapter 上方：

| 原行为 | 风险 |
|---|---|
| `MAX_CONTEXT_TOKENS=100000`、`MAX_OUTPUT_TOKENS=8000` 作为所有模型默认 | 1M Gemini/Qwen 会过早压缩，小窗口或 reasoning model 又可能预留错误 |
| 所有模型共用一份 system prompt | 无法表达 Codex、Anthropic、Gemini、Qwen 的交互侧重点 |
| 主 Loop 只按调用方 `stream=True/False` 选择路径 | 配置到不支持 stream 的代理模型时没有稳定降级 |
| 主 Loop 总是提供工具 schema | text/image-only 模型仍收到不支持的 tools 字段 |
| `PASS_REASONING_CONTENT` 是未正式配置的全局开关，默认 true | 普通模型可能收到不认识的字段；需要 interleaved reasoning 的模型又没有显式声明 |
| OpenAI-compatible adapter 原样发送 `max_tokens` | GPT-5/Codex 兼容端点可能要求 `max_completion_tokens` 并拒绝 temperature |
| trace 只记录 model id | 无法知道一次 run 实际使用了哪组窗口与 capability |

### 17.3 实现结果

现在的模型请求链路是：

```text
MODEL_PROVIDER + MODEL_ID
  -> ordered built-in capability rules
  -> provider family correction (native Anthropic / native Gemini)
  -> explicit MAX_CONTEXT_TOKENS / MAX_OUTPUT_TOKENS override（若环境显式设置）
  -> MODEL_CAPABILITIES_JSON override（私有模型/代理）
  -> immutable ModelCapabilities

ModelCapabilities
  -> runtime/prompt.py: shared base prompt + family appendix
  -> AgentLoop: context budget / tools / stream fallback / reasoning history
  -> OpenAICompatibleProvider: temperature policy + token-limit field mapping
  -> subagent: child model budget / tools / family guidance
  -> run_start trace: resolved capability snapshot
```

#### 统一能力记录

`providers/capabilities.py` 新增 frozen `ModelCapabilities`：

| 字段 | 用途 |
|---|---|
| `provider`, `model_id`, `family` | 模型身份和归类 |
| `prompt_family` | 选择 default/anthropic/gemini/gpt/codex/qwen family appendix |
| `context_tokens`, `output_tokens` | 驱动现有 `prompt_budget()` 的窗口与输出预留 |
| `supports_tools`, `supports_streaming` | 决定是否发送 tools、是否从 streaming 稳定降级到 complete |
| `supports_reasoning` | capability/trace 描述，不等同于自动开启所有 provider 的 thinking mode |
| `supports_temperature`, `default_temperature` | OpenAI-compatible 请求是否发送及默认 temperature |
| `preserve_reasoning_content` | 是否把 provider 的 interleaved reasoning 字段带回下一轮历史 |
| `max_tokens_parameter` | `max_tokens` 或 `max_completion_tokens` |
| `source` | `builtin:<rule>`、`fallback` 以及是否叠加 override |

ordered rules 先匹配更具体的 GPT-5 chat、Codex、reasoning、Gemini image、Qwen thinking 等组合，再匹配
GPT、Claude、Gemini、Qwen、DeepSeek、GLM 和 Kimi 家族。未知模型使用原有 100K/8K fallback，不因为 registry
无法识别就拒绝启动。

内置数字是本地保守默认，不是在线事实库。若 `.env` 显式设置 `MAX_CONTEXT_TOKENS` 或
`MAX_OUTPUT_TOKENS`，它们覆盖 family 默认；私有 gateway 可用 `MODEL_CAPABILITIES_JSON` 覆盖 family、prompt、
五项 bool 能力、reasoning 往返、token 参数名和 temperature。override 会校验未知字段、identity 字段、bool 类型和
token 参数名，错误配置会在启动时给出明确 `ValueError`，不会静默拼错请求。

#### Prompt family

`runtime/prompt.py` 保留现有共享工具/安全/验证主提示，只根据 capability 追加一个小型 family appendix：

- Anthropic：原生 structured tool call 和 call/result 顺序；
- Gemini：严格 function argument 与 provider metadata 往返；
- Codex：自主工具推进、最小已验证 patch、不要展示隐藏 reasoning；
- GPT：短工具驱动过程和严格 JSON；
- Qwen：严格 JSON，工具结果后不要重复整份计划。

这比 InfCode 的多份完整 prompt 文件更保守，避免一次切换重写 NZ-Coder 已验证的行为提示。它已经让 prompt 选择
依赖模型能力，但仍属于第一阶段。

#### AgentLoop 与上下文预算

`AgentLoop.__init__()` 为本次 Agent 解析并保存一份 immutable capability。`_prompt_budget()` 把当前模型
context/output 交给已有模型窗口预算器；auto compact、超长用户输入持久化、streaming 和 non-streaming 主请求因此
使用同一输出预留。为了兼容现有嵌入方和用 `AgentLoop.__new__()` 构造的测试 fake，深层方法在缺少新字段时仍回退
到 `config.MODEL_ID` 与原 `prompt_budget()`。

当调用方请求 stream 但 capability 声明不支持时，Loop 改走 non-streaming，并记录
`provider_capability_fallback`。不支持 tools 的模型收到空工具列表；OpenAI-compatible adapter 会进一步删除 tools/
tool_choice 字段。支持 interleaved reasoning 的 Qwen/DeepSeek/GLM 等保留 `reasoning_content`，普通 GPT 模型在下一轮
请求前剥离它，替代旧的进程级全局判断。

`run_start` 现在记录 model、family、prompt family、context/output、tools/stream/reasoning 和 capability source，后续
看到溢出或 provider 400 时能够复原实际请求策略。

#### 请求参数策略

`prepare_openai_request()` 是当前第一阶段的 provider request transform：

- capability 不支持 tools/stream/temperature 时删除对应字段；
- 声明默认 temperature 的 OpenAI-compatible family 在调用方未显式传值时补默认值；
- `max_tokens_parameter=max_completion_tokens` 的 capability 执行字段映射；当前覆盖 Codex、非 chat GPT-5 与
  o1/o3/o4，`gpt-5-chat` 特例仍保留 `max_tokens` 和 temperature；
- 不修改 messages 和 tool schema 内容，协议级转换仍由各 adapter 自己负责。

原生 Anthropic/Gemini adapter 暴露同一个 `capabilities(model_id)` 合同，但仍在各自 payload builder 中把统一
`max_tokens` 转成原生字段。这样没有把 OpenAI 字段名硬塞进 native API。

#### 子 Agent

手写子 Agent loop 会按 `state["model_id"]` 单独解析 capability，因此 `SUBAGENT_EXPLORE_MODEL` 与父模型不同时，
输出预算、tools 和 prompt appendix 也随 child model 改变，不再错误沿用父 Agent 的固定 100K/8K 假设。

### 17.4 关键设计决策

#### capability 与 adapter 分层

adapter 解决“怎样发请求、怎样把响应变成统一对象”；capability 解决“这个模型允许发送什么、应预留多少、用哪类
提示”。把 metadata 直接散落到 Anthropic/Gemini/OpenAI 类里会让 AgentLoop 重新依赖 adapter 类型判断。

#### immutable snapshot，而不是运行时可变字典

一次 Agent 的模型能力应在构造时确定。frozen dataclass 防止某个请求临时改 `supports_tools` 后污染后续 turn，也与
A014 的 per-execution 隔离方向一致。JSON override 最终也生成新 dataclass，而不是保存共享 dict 引用。

#### family 默认只作为 fallback

同一个 model id 在不同 provider 上可能有不同窗口、输出和功能开关；静态 pattern 永远不可能等价于 InfCode 的
provider-specific models.dev 记录。因此环境显式 limit 和 JSON override 优先，source 字段进入 trace，文档也不把
内置数字描述为实时官方规格。

#### 保留现有三个 adapter

原生 Anthropic/Gemini 的 stream、tool 和 metadata round trip 已有测试。为了“看起来像 InfCode”而换成另一套抽象会
制造回归且违反最小改动原则。本项只为它们增加 capability contract，并把共享策略放在上层。

### 17.5 关键文件

- `nz_coder/providers/capabilities.py`：模型能力记录、family rules、override、prompt appendix 和 OpenAI request transform；
- `nz_coder/providers/base.py`：`ModelProvider` protocol 增加 capability contract；
- `nz_coder/providers/openai_compatible.py`：请求发送前应用 capability transform；
- `nz_coder/providers/anthropic.py`、`gemini.py`：暴露 native model capability；
- `nz_coder/providers/__init__.py`：公开 registry API；
- `nz_coder/runtime/prompt.py`：共享主提示叠加 family guidance；
- `nz_coder/runtime/loop.py`：绑定 capability、模型预算、tools/stream/reasoning 策略和 trace；
- `nz_coder/runtime/subagent.py`：按 child model 解析能力；
- `nz_coder/config.py`、`.env.example`：`MODEL_CAPABILITIES_JSON` 配置与示例；
- `tests/test_model_capabilities.py`：registry、override、request mapping、prompt、budget、fallback 和 reasoning 测试。

### 17.6 验证结果

完成本项时：

- Provider、Context Budget 和 AgentLoop 聚焦回归：`80 passed`；
- Subagent、Smoke、evaluation 与 runtime context 扩展回归：`70 passed`；
- 完整测试套件：`556 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- A015 新模块和小文件严格 Ruff：`All checks passed!`；
- `runtime/loop.py`、`runtime/subagent.py` 使用既有兼容忽略项后 Ruff：`All checks passed!`；
- A015 修改源码和测试 `py_compile` 通过；
- `git diff --check` 通过；
- 所有 Provider 测试均为离线 fake transport/client，没有发送真实 API 请求；
- 没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q \
  tests/test_model_capabilities.py tests/test_providers.py \
  tests/test_native_providers.py tests/test_provider_smoke.py \
  tests/test_context_budget.py tests/test_loop_fake.py
python3 -m pytest -q \
  tests/test_subagent.py tests/test_smoke.py tests/test_eval_runner.py \
  tests/test_dodo_dev_headless.py tests/test_execution_context.py
python3 -m ruff check \
  nz_coder/providers/capabilities.py nz_coder/providers/base.py \
  nz_coder/providers/openai_compatible.py nz_coder/providers/anthropic.py \
  nz_coder/providers/gemini.py nz_coder/providers/__init__.py \
  nz_coder/runtime/prompt.py tests/test_model_capabilities.py
python3 -m py_compile \
  nz_coder/providers/capabilities.py nz_coder/providers/base.py \
  nz_coder/providers/openai_compatible.py nz_coder/providers/anthropic.py \
  nz_coder/providers/gemini.py nz_coder/providers/__init__.py \
  nz_coder/runtime/prompt.py nz_coder/runtime/loop.py \
  nz_coder/runtime/subagent.py tests/test_model_capabilities.py
python3 -m pytest -q
```

### 17.7 学习重点

1. Provider adapter 与 model capability 是两层问题：协议相同不代表窗口、reasoning 或请求字段相同。
2. context budget 必须消费当前模型记录；只把窗口写进配置文件但深层仍读固定常量，不算真正模型感知。
3. capability 应决定请求字段是否存在，而不仅仅用于 UI 展示。
4. interleaved reasoning 是多轮协议状态；对不支持的模型发送会报错，对需要的模型丢弃会破坏 tool round trip。
5. family pattern 是离线 fallback，不是可靠的在线模型目录；必须提供显式 override 和可观测 source。
6. 一次迁移不必推翻已通过测试的 adapter。先增加共享 metadata contract，能以更小 diff 收敛架构。

### 17.8 设计边界与剩余差距

当前接受的边界：

- registry 是手写 family rules，不会自动同步 models.dev，也没有 provider 动态 discovery、模型状态/成本/modality 数据；
- 仅有 OpenAI-compatible、Anthropic、Gemini 三个 adapter，尚未覆盖 InfCode 的 Bedrock、Azure、OpenRouter、Vertex、
  xAI、Mistral、Groq 等 provider；
- prompt family 是共享主提示加小 appendix，不是 InfCode 的 Anthropic/Gemini/GPT/Codex/Kimi 等完整独立 prompt；
- `supports_reasoning` 当前用于描述、trace 和 reasoning 历史策略，没有实现所有 provider 的 reasoning effort、thinking
  budget、encrypted content、cache key 或 variants；
- OpenAI request transform 只覆盖 tools/stream/temperature 与 token-limit 字段，没有实现 InfCode 的 top-p/top-k、
  provider options、headers、prompt cache 和 Responses API；
- 原有 memory/compaction summary 的若干直接 `client.chat.completions.create()` 仍未全部迁移到 provider transform；
- tool-less 模型可以稳定完成 text response，但 NZ-Coder 的主 system prompt 仍以 coding tools 为中心，不等于完整的
  text-only chat 产品模式；
- 内置 limit 会随模型版本变化；生产使用私有 gateway 时应显式配置 limits/JSON override，并用 provider smoke 验证；
- 本项没有发送真实 API 请求，因此只证明请求形状、预算和降级路径，不证明每个远程服务当前接受这些参数。

下一步建议进入 MCP 第一阶段：先实现 stdio server 的配置、进程生命周期、工具发现/调用，以及 MCP tool 到现有
read/serial/write effect 和权限边界的映射。

## 18. A016：MCP 本地 stdio 工具协议

### 18.1 InfCode 参考能力与准确边界

MCP（Model Context Protocol）是 coding agent/client 与外部 server 交换工具 definition/schema、调用参数和结果的协议；
它不是 Agent 框架。本项重新阅读了当前工作区中的 InfCode MCP 实现，主要参考：

- `infcode-dev/infcode-dev/packages/opencode/src/mcp/index.ts`；
- 其中的 `convertMcpTool()`、`defs()`、`connectTransport()`、`connectLocal()`、`create()`、`watch()`、
  instance finalizer 和 tool name 组装逻辑。

InfCode 使用官方 MCP TypeScript SDK。它能为 local server 创建 `StdioClientTransport`，在 instance 目录启动命令，
执行 connect/initialize，调用 `listTools()` 并把 definition 转成动态 AI SDK tool；执行时再调用 `client.callTool()`。
当前 InfCode 还具有 connected/connecting/disabled/failed/auth 状态、后台启动、请求 timeout、tool list change 通知、
prompts/resources 缓存、Streamable HTTP/SSE、OAuth、运行时 reconcile，以及 instance finalizer/显式 disconnect 的资源清理。

因此，本项“第一阶段对齐”只指 local stdio 的最小闭环：配置、initialize、工具发现、工具调用、超时、失败隔离和退出清理。
它不表示已经复制 InfCode 的完整 MCP 产品能力。

### 18.2 NZ-Coder 原有状态与实际不足

A016 之前，NZ-Coder 的 `register()`/`dispatch()` 只接受 Python 模块副作用 import 注册的本地工具，optional pack 也仍是
Python import。用户无法连接一个标准 MCP server，模型也无法看到该 server 的工具 schema。

直接把 MCP definition 注册进模块级 `TOOL_SPECS`/`TOOL_HANDLERS` 也不可接受：A014 已允许同一进程并发运行不同
workspace/session；若全局注册 handler，后注册的 server 会覆盖先注册的同名工具，并可能让 Agent A 调用 Agent B 的
stdio 客户端。除此之外还缺少以下边界：

- local command 是否经过 shell、cwd 是否逃逸 workspace；
- request ID、超时、异常退出和 stderr drain；
- 多个 server 中一个启动失败时是否拖垮整个 Agent；
- MCP tool 如何进入 read/serial/write 调度和权限；
- 外部写入不受 `TransactionManager` 回滚时如何避免误导；
- server 输出如何标明为外部不可信内容；
- run 结束、异常或取消时是否可靠终止子进程。

### 18.3 实现结果

核心调用链：

```text
AgentLoop.run()
  -> MCPRuntime.configured(workspace)
  -> 校验 NZ_MCP_SERVERS_JSON
  -> MCPRuntime.start()（通过 asyncio.to_thread 离开 event loop）
  -> MCPClient.start()
     -> subprocess.Popen(argv, shell=False；POSIX 使用 start_new_session=True)
     -> initialize request
     -> notifications/initialized
     -> tools/list（最多 100 页）
  -> MCPRuntime 将 definition 转为 binding
  -> scoped_dynamic_tools() 绑定当前 ContextVar
  -> provider 每轮从 get_specs() 看到 MCP schema
  -> 既有 PermissionManager + effect-aware scheduler
  -> dispatch() -> tools/call
  -> 不可信结果进入既有大输出持久化/trace/messages 流程
  -> finally: MCPRuntime.close() -> POSIX 终止独立进程组 / Windows 终止直接子进程
```

#### 配置与启动安全

MCP 默认关闭，只有 `NZ_MCP_ENABLED=1` 才解析并启动配置。示例：

```dotenv
NZ_MCP_ENABLED=1
NZ_MCP_SERVERS_JSON={"servers":{"local":{"command":["python3","tools/mcp_server.py"],"cwd":".","tool_effects":{"search":"read","update":"write"}}}}
NZ_MCP_STARTUP_TIMEOUT_SECONDS=30
NZ_MCP_TOOL_TIMEOUT_SECONDS=30
```

`command` 必须是非空字符串数组，直接传给 `Popen`，不会拼成 shell command。server name、env key、bool、正 timeout、
未知字段和 effect 都会严格校验；`cwd` 可以是相对或绝对路径，但 resolve 后必须位于当前 workspace 内。环境变量仍以
宿主 `os.environ` 为底，再叠加配置中的 `env`，这与 InfCode local transport 的继承行为接近，也意味着只应启用可信的
本地 MCP server。

每个 server 独立启动。一个命令不存在、initialize 超时或 tools/list 无效，只会产生该 server 的 `failed` 状态，
其他 server 仍可连接。trace 中的 status 不记录 command/env/stderr，只记录 server name、状态、工具数和异常类型，
避免把可能含 secret 的启动信息写入运行日志。

#### 手写 JSON-RPC stdio client

在“不新增 MCP SDK 依赖”的约束下，`MCPClient` 使用标准库实现 MCP stdio 所需的 JSON-lines JSON-RPC 2.0 子集：

- 单调 request ID 和 pending queue 映射，允许同一 client 相关联多个并发请求；
- stdout/stderr 独立 daemon reader，stderr 使用 50 行 bounded tail 防止 pipe 堵塞和无限增长；
- initialize、initialized notification、分页 tools/list 和 tools/call；
- startup/tool 两种 deadline，超时抛出明确 `MCPTimeoutError`；
- stdout 非法 JSON、异常 EOF 或 transport error 会唤醒全部 pending 请求，后续调用快速失败；
- server 反向发来的 client request 返回 JSON-RPC `-32601`，而不是悬挂；
- POSIX 使用新 session/process group，正常退出先 SIGTERM，超时后 SIGKILL；Windows 终止直接子进程；run 的
  `finally` 总会调用 close，Runtime 也跟踪尚在 startup 的 in-flight client，避免取消启动时漏掉它。

#### Context-local 动态工具

`tools/__init__.py` 新增 ContextVar dynamic overlay。definition 复用内建工具的 name/description/parameters/handler/effect
形状，但不会修改 `TOOL_SPECS`、`TOOL_HANDLERS` 或 `TOOL_EXECUTION_MODES`。`get_specs()`、`dispatch()` 和
`get_execution_mode()` 先查询当前 overlay；退出 context 后绑定立即消失。嵌套 scope 和 scheduler worker 的
`copy_context()` 保持现有 A014/A007 隔离语义。

公开名使用 `mcp_<server>_<tool>`，不合法字符转下划线，变换、截断或冲突时加入稳定 SHA-256 短摘要。这个 `mcp_`
前缀是 NZ-Coder 有意增加的权限命名空间；InfCode 当前使用 `<server>_<tool>`。MCP input schema 被复制并强制为 object，
无效 properties 回退为空对象，`additionalProperties=false`，与 InfCode `convertMcpTool()` 的保守 schema 方向一致。

#### Effect、权限与事务边界

每个 MCP tool 可显式声明 `read`、`serial` 或 `write`；未声明时保守默认为 `serial`。

| 模式 | MCP read | MCP serial | MCP write |
|---|---|---|---|
| `default` | allow | ask | ask |
| `acceptEdits` | allow | ask | ask |
| `plan` | allow | deny | deny |
| `auto` | allow | allow | allow |

deny rule 始终优先；default/acceptEdits 下可用显式 allow rule 放行某个 MCP 工具。Plan mode 的非 read 禁止不能被 allow
rule 绕过。`read` 进入既有连续只读并发段，`serial/write` 都形成顺序屏障。

这里最重要的修正是把“调度 effect”和“本地事务写入”拆开。MCP `write` 代表外部系统副作用，仍用于审批和串行屏障，
但 binding 带有 `transactional=False`。因此它不会启动本地 `TransactionManager`、不会触发 ChangeTracker/LSP 写后诊断/
代码验证门，也不会在失败时声称外部操作已经回滚。普通动态 write 的默认 `transactional=True` 保持 A007 兼容。

#### 输出与子 Agent 边界

成功的 MCP tool result 包装为带 `untrusted=true` 的 `<mcp-output>`；`isError` 或 transport/protocol 错误保持 `Error:`
前缀，以便现有 ToolExecutor 正确分类。文本、structuredContent 和非文本 content 都能转成字符串，并继续经过 NZ-Coder
已有的大输出落盘与 preview 逻辑。

第一阶段不让 child Agent 的执行 scope 继承父 Agent 的 MCP binding。由于 ContextVar 会自然传播到 worker，child loop
显式进入 `scoped_dynamic_tools_disabled()` 清空 overlay；`_subagent_tools()` 还会过滤 `mcp_`，形成 schema allowlist 的
第二道防线。这样 child 无法取得父级 handler/client。未来若支持子 Agent MCP，应由 child 按自己的 workspace/session
建立独立 client。

### 18.4 关键设计决策

#### 不进入全局 registry

MCP client 是带 subprocess、pending request 和关闭状态的实例资源，不是纯函数 handler。用 ContextVar overlay 可以复用
现有 provider schema、dispatch 和 scheduler，同时不破坏 A014 的 workspace/session 隔离，也不需要重写注册器接口。

#### 手写协议子集，而不是加入 SDK

项目约束只允许现有少量依赖，并强调不引入 Agent 框架。本阶段只需要四个 MCP 方法和 JSON-RPC correlation；标准库实现
能保持依赖边界清楚。代价是协议兼容面明显小于官方 SDK，必须在“剩余差距”中明确，而不能把它描述成完整 MCP。

#### MCP write 不等于本地 write

调度器需要知道一个工具是否可以并行，权限系统需要知道它是否有外部副作用；TransactionManager 需要回答的却是“本地
文件能否快照并回滚”。用同一个 `is_write` bool 表示三者会产生错误承诺，因此 dynamic binding 增加独立
`transactional` 元数据，但 provider tool schema 完全不变。

#### 每个 Agent run 持有生命周期

当前 NZ-Coder 没有 InfCode 的长期 instance service。把 runtime 绑定到 `AgentLoop.run()`，并在 `finally` 关闭，是现有架构下
最容易证明无泄漏的所有权模型。它会导致多次 run 重启 server，这是第一阶段接受的性能差距。

### 18.5 关键文件

- `nz_coder/mcp/config.py`：server 配置 dataclass、JSON 校验、workspace cwd 和 effect；
- `nz_coder/mcp/client.py`：JSON-lines JSON-RPC、请求关联、timeout、reader 和进程组清理；
- `nz_coder/mcp/runtime.py`：server 状态、失败隔离、tool binding、名称/schema 和结果格式；
- `nz_coder/mcp/__init__.py`：MCP 公开 API；
- `nz_coder/tools/__init__.py`：ContextVar dynamic overlay 与 transactional metadata；
- `nz_coder/tool_platform/permissioning/checker.py`：MCP 专属 Plan/default/acceptEdits/auto 策略；
- `nz_coder/runtime/tool_executor.py`：调度 write 与本地 transactional write 的区分；
- `nz_coder/runtime/loop.py`：per-run start/bind/status/finally close；
- `nz_coder/runtime/subagent.py`：第一阶段过滤父 Agent MCP 工具；
- `nz_coder/config.py`、`.env.example`：enable、server JSON 和 timeout 配置；
- `tests/fixtures/mcp_echo_server.py`：真实 subprocess fake MCP server；
- `tests/fixtures/mcp_orphan_server.py`：leader 退出后遗留 SIGTERM-resistant child 的 POSIX 清理夹具；
- `tests/test_mcp.py`：配置、transport、runtime、权限、线程隔离、子 Agent 和 Loop 生命周期测试。

### 18.6 验证结果

完成本项时：

- MCP、权限、调度、子 Agent 与 Loop 聚焦回归：`88 passed`；
- 完整测试套件：`579 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 新增/修改 Python 文件 `py_compile` 通过；
- A016 相关文件 `git diff --check` 通过；
- 使用测试目录中的真实 Python stdio 子进程完成 initialize、tools/list、tools/call、structured result、tool error、
  timeout、严格有限 timeout 校验、启动取消、异常 server 隔离、子 Agent overlay 清空、leader 先退出后的 descendant
  process-group 清理和直接进程退出验证；
- 没有连接第三方公开 MCP server，没有测试 HTTP/SSE/OAuth；
- 按用户要求，没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m pytest -q \
  tests/test_mcp.py tests/test_permissions.py tests/test_parallel_benchmark.py \
  tests/test_subagent.py tests/test_loop_fake.py
python3 -m py_compile \
  nz_coder/mcp/__init__.py nz_coder/mcp/config.py \
  nz_coder/mcp/client.py nz_coder/mcp/runtime.py \
  nz_coder/tools/__init__.py nz_coder/runtime/tool_executor.py \
  nz_coder/runtime/loop.py tests/test_mcp.py \
  tests/fixtures/mcp_echo_server.py tests/fixtures/mcp_orphan_server.py
python3 -m pytest -q
```

### 18.7 学习重点

1. MCP 是客户端与外部 context/tool server 之间的协议，不是 Agent 框架；接入 MCP 不需要替换 AgentLoop。
2. stdio MCP 使用一行一个 JSON-RPC message；它与 LSP 的 `Content-Length` framing 不同，不能复用同一分帧器。
3. 动态 tool definition 不只是 schema，还携带活的 client/lifecycle；并发 Agent 下不能放进模块级全局 registry。
4. `read/serial/write` 回答调度与权限问题，`transactional` 回答本地回滚问题，两者不能混为一个 bool。
5. timeout 之后必须从 pending map 移除 request；transport 崩溃必须唤醒所有等待者，不能让每个调用各等一次 timeout。
6. child process 生命周期必须有明确 owner；`finally`、stderr drain、进程组 SIGTERM/SIGKILL 都是功能正确性的一部分。
7. MCP server 与输出都属于外部信任边界。显式启用、严格 cwd/argv、保守 effect、status trace 脱敏和 untrusted 标记
   缺一不可；普通 tool-result trace 仍会保存 server 返回内容，不应把 trace 目录当作无敏感数据区域。

### 18.8 设计边界与剩余差距

当前明确未对齐 InfCode 的部分：

- 只支持 local stdio；没有 Streamable HTTP、SSE、OAuth、headers、auth 状态和 token storage；
- 只实现 initialize、tools/list、tools/call；没有 prompts、resources、sampling、roots、logging、progress、cancellation；
- 不监听 tools/list_changed，也没有运行时 reconcile、enable/disable/connect/disconnect 管理 API；
- server 在每次 `AgentLoop.run()` 前顺序启动并等待完成，不像 InfCode 后台并行连接；多个慢 server 会累加启动延迟；
- run 结束就关闭 server，下一次 run 会重启；尚无 workspace/session 长生命周期 MCP manager；
- 子 Agent 暂不继承 MCP，也没有为 child 创建独立 MCP runtime；
- 继承宿主环境变量，可信 server 可以读取进程中的 API key；目前没有 env allowlist 或 secret broker；
- 手写 client 没有官方 SDK 的完整 schema/version negotiation/兼容测试矩阵；
- 非文本 content 只序列化为文本，没有把 image/audio/resource link 作为 provider multimodal content 传递；
- tool 名增加 `mcp_` 前缀，和 InfCode 的 `<server>_<tool>` 不完全相同，这是权限路由与来源可识别性的有意差异；
- status trace 为避免 secret 只保留异常类型，诊断体验弱于 InfCode 的详细错误和 UI 状态；
- MCP tool result/error 仍按普通工具结果进入 messages 和 trace，可能包含 server 返回的敏感内容；
- 当前真实链路使用仓库内 fake server，尚未对常见第三方 MCP server 做互操作冒烟。

下一步不建议立即铺开 HTTP/OAuth。更合理的是先实现统一 Session 事件协议，或者先把 MCP manager 提升为
workspace/session 生命周期并支持非阻塞并行 startup/list-changed；选择前仍需重新阅读当时的 InfCode 源码。

## 19. A017：原生 Session 事件协议与 Dodo 核心解耦

### 19.1 InfCode 参考能力与准确边界

本项重新阅读了当前工作区中的 InfCode 事件与服务实现，主要参考：

- `packages/opencode/src/bus/index.ts`：instance-local Bus、typed/wildcard PubSub、publish/subscribe/unsubscribe 和跨实例 envelope；
- `packages/opencode/src/bus/bus-event.ts`：事件 definition 注册；
- `packages/opencode/src/server/routes/instance/event.ts`：SSE `server.connected`、10 秒 heartbeat、订阅取消和连接清理；
- `ARCHITECTURE.md`：HTTP/SSE server、SDK、GUI Bridge 与 IDE host 的分层边界。

InfCode 的关键点不是“把 trace 发给 UI”，而是核心运行时先发布稳定的 session domain event，再由 SSE、SDK 和宿主消费。
事件流有明确的 instance 生命周期；SSE 断开时会解除订阅，避免客户端生命周期反向污染核心 Agent。

A017 只对齐这条链路的第一层：实例级事件总线、事件 envelope、订阅生命周期和 SSE framing。NZ-Coder 尚未实现 InfCode 的
HTTP server、数据库 session parts、生成 SDK、GUI Bridge、VS Code/JetBrains host，也没有把本地 SSE iterator 描述成完整
客户端生态。

### 19.2 NZ-Coder 原有状态与实际不足

A017 之前，NZ-Coder 有 `TraceRecorder`、streaming token callback 和多个入口自己的状态接口，但没有统一客户端协议：

- trace 是面向事后诊断的 JSONL，字段由内部实现驱动，不适合作为稳定的 live client contract；
- CLI、headless、PySide 和 Dodo 各自接入 Agent，run/tool/session 状态没有共同 envelope；
- tool worker 可以并行完成，但没有线程安全的实例级 fan-out；
- 没有订阅取消、有界背压、近期回放、connected/heartbeat 等 transport 基础；
- Dodo-specific trace 选择、内存 backend 初始化和环境配置曾进入 core `AgentLoop`、`MemoryManager`、`config.py`，让一个借入的
  外部控制面看起来像 NZ-Coder 的默认架构；
- 默认安装还暴露 Dodo PySide console script，主 `.env.example` 混入 Dodo server/auth/embedding 配置。

这不只是目录整洁问题。核心按环境变量自动导入 Dodo，会让默认行为依赖外部架构；而直接把 message/tool result 自动镜像到
远端，还会新增源码、凭据和用户数据外传边界。

### 19.3 实现结果

核心调用链：

```text
AgentLoop.__init__()
  -> 创建 SessionEventBus(session_id)
  -> bind run_id / agent_id

AgentLoop.run()
  -> scoped_session_event_bus(event_bus)
  -> session.run.started
  -> session.mcp.status
  -> session.message.completed
  -> session.tool.completed
  -> session.run.completed / failed / cancelled

subscriber
  -> subscribe(type filter, max_queue, replay)
  -> SessionEvent(type, properties, meta)
  -> encode_sse() / iter_sse()
  -> server.connected / event / server.heartbeat

AgentLoop.close() 或 CLI session replacement
  -> session.disposed
  -> 关闭全部 subscription
```

#### 稳定事件 envelope

`SessionEvent` 是 immutable dataclass。wire shape 固定为：

```json
{
  "type": "session.tool.completed",
  "properties": {},
  "meta": {
    "schema_version": 1,
    "event_id": "...",
    "sequence": 3,
    "timestamp": 1785.0,
    "session_id": "...",
    "run_id": "...",
    "agent_id": "..."
  }
}
```

事件名只允许受限的 lowercase dotted token；`properties` 必须是 object。发布和 `to_dict()` 都进行 deep copy，调用方在发布后
修改原字典，或消费方修改序列化结果，都不会篡改 replay 中保存的事件。每个 bus 的 sequence 单调递增，event ID 全局随机，
使客户端可以区分顺序与事件身份。

#### 实例隔离、背压与回放

每个 `AgentLoop` 持有自己的 `SessionEventBus`，没有模块级 session registry。`ContextVar` 只暴露当前运行中的 bus，嵌套 scope
退出后恢复上一层；它与 A014 的 workspace/session 隔离方式一致。

publish 在锁内分配 sequence 和更新 bounded replay buffer，锁外向订阅者 fan-out。每个 subscription 有独立 bounded queue；
慢消费者队列满时丢最旧事件并增加 `dropped_events`，不会阻塞模型或工具 worker。订阅可以过滤 event type，并在创建时回放
最近 N 条匹配事件。关闭 subscription 会从 bus 解除注册；关闭 bus 先发布 `session.disposed`，再唤醒并结束全部等待者。

#### AgentLoop 与 CLI 生命周期

AgentLoop 发布 run、MCP status、完整模型消息和工具完成事件；异常、async cancellation、正常完成和 verification abort 有不同
终态。事件发布是 best effort，客户端故障不能改变 Agent 的工具结果或事务结论。

`AgentLoop.close()` 统一关闭 EventBus 和 tracer。CLI 正常退出会 close 当前 Agent；`/new-session` 和 `/resume` 通过
`CommandContext.replace_agent()` 关闭旧实例。`run()` 还为绕过构造函数的旧夹具惰性补建 bus，避免破坏已有接口。

#### Dodo 能力内化与兼容隔离

本项没有把 Dodo 外壳重命名后继续塞进核心，而是只内化可复用机制：

- live client event 由项目自有 `session_events.py` 实现，不依赖 Dodo；
- core `MemoryManager` 改为通用 `store`/`sync` 注入，并保留 `backend_status()`、transaction binding 等能力；
- `dodo/memory_adapter.py` 负责读取 Dodo 环境并把 HybridMemoryStore/MemorySync 注入核心，依赖方向由 Dodo 指向 core；
- core `AgentLoop` 不再根据 `DODO_TRACE_ENABLED` 自动选择 tracer；Dodo headless 默认也走 core tracer；
- Dodo 配置从 `config.py` 和主 `.env.example` 移除，`nz-dodo-client` 从默认 console scripts 移除。

旧 scheduler/ingress/server/PySide/DodoTraceRecorder 文件暂时保留在 `nz_coder/dodo/` 兼容区，避免一次性删除用户可能仍在调用的
代码；它们不再代表目标架构。没有实现 SessionEvent 到 Dodo 的自动网络 mirror：这些 payload 包含模型内容和工具输出，若未来
需要外部 sink，必须另做显式 opt-in、脱敏、授权和失败隔离设计。

### 19.4 关键设计决策

#### EventBus 与 trace 分层

trace 服务开发者事后诊断，可以记录内部调度字段并持久化；Session event 服务客户端实时同步，需要稳定 envelope、实例 identity
和订阅生命周期。把 trace JSONL 行原样广播会把内部实现永久变成客户端 API，因此两者保留独立职责。

#### 有界队列不阻塞 Agent

InfCode 的 Bus 重点是 publish/subscribe 生命周期。NZ-Coder 的工具调度还会在线程池发布事件，因此额外采用每订阅者有界队列和
drop-oldest。客户端丢事件可以通过 `dropped_events` 发现；让慢 UI 阻塞事务提交或模型循环则不可接受。

#### 先提供 transport primitive，不伪造 server

`encode_sse()`/`iter_sse()` 负责标准 data frame、connected 和 heartbeat，但当前没有 HTTP route、disconnect signal 或 socket
write failure。这样能先冻结核心协议，下一阶段再让真正 server 持有 subscription，而不是让 AgentLoop 依赖某个 Web 框架。

#### 不自动接入外部事件 sink

Dodo trace mirror 属于外部数据传输，不是本地 EventBus 的自然延伸。即使借入代码能工作，也不能在用户只要求“对齐 InfCode”时
把完整 message/tool result 自动发送到远端。默认本地、显式建立信任边界，是本项的安全约束。

### 19.5 关键文件

- `nz_coder/session_events.py`：event dataclass、实例 bus、subscription、ContextVar 和 SSE framing；
- `nz_coder/runtime/loop.py`：EventBus 所有权与 run/message/tool/terminal events；
- `nz_coder/interface/cli.py`：REPL 退出时关闭当前 Agent；
- `nz_coder/interface/commands/registry.py`：session replacement 关闭旧 Agent；
- `nz_coder/state/memory.py`：通用 store/sync 注入，不再导入 Dodo；
- `nz_coder/dodo/memory_adapter.py`：隔离的 Dodo memory compatibility adapter；
- `nz_coder/dodo/headless.py`、`nz_coder/dodo/dev_headless.py`：显式选择 Dodo adapter 的 legacy 入口；
- `nz_coder/config.py`、`.env.example`、`pyproject.toml`：Dodo 从 core/default install 配置面退出；
- `tests/test_session_events.py`：envelope、隔离、线程并发、背压、回放、SSE 和 AgentLoop 生命周期；
- `tests/test_cli_commands.py`、`tests/test_dodo_integration.py`：session replacement close 与 core/Dodo 依赖方向。

### 19.6 验证结果

完成本项时：

- A017 修改文件严格 Ruff：`All checks passed!`；
- 新增/修改核心 Python 文件 `py_compile` 通过；
- Session/CLI/Memory/Dodo/Loop/MCP 聚焦回归：`97 passed`；
- 完整测试套件：`586 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 使用线程并发 publish 验证 sequence 唯一有序，使用本地 iterator 验证 connected/event/heartbeat/close；没有启动真实 HTTP server；
- 生产 core 的 Loop/Memory/config/default script 不再自动依赖 Dodo，legacy Dodo vertical slice 仍通过全量回归；
- 按用户要求，没有运行 SWE-bench 官方评测，也没有向任何外部 Dodo 服务发送事件。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/session_events.py nz_coder/runtime/loop.py \
  nz_coder/state/memory.py nz_coder/dodo/memory_adapter.py \
  nz_coder/interface/cli.py nz_coder/interface/commands/registry.py \
  tests/test_session_events.py tests/test_cli_commands.py tests/test_dodo_integration.py
python3 -m py_compile \
  nz_coder/session_events.py nz_coder/runtime/loop.py \
  nz_coder/state/memory.py nz_coder/dodo/memory_adapter.py \
  nz_coder/interface/cli.py nz_coder/interface/commands/registry.py
python3 -m pytest -q \
  tests/test_session_events.py tests/test_cli_commands.py tests/test_memory.py \
  tests/test_dodo_integration.py tests/test_dodo_dev_headless.py \
  tests/test_loop_fake.py tests/test_mcp.py
python3 -m pytest -q
```

### 19.7 学习重点

1. 客户端协议应表达 session domain event，而不是直接暴露内部 trace 行。
2. EventBus 的作用域必须与 Agent 实例一致；模块级 subscriber registry 会重演 workspace 串线问题。
3. sequence、event ID、schema version 和 session/run/agent identity 解决不同问题，不能只留一个 timestamp。
4. 多消费者 fan-out 必须单独处理慢消费者；一个 UI 的背压不能暂停模型和事务。
5. ContextVar 适合暴露当前实例资源，但资源所有权仍属于 AgentLoop，必须有 close/dispose 终点。
6. “内化借入架构”应提取通用 contract 并反转依赖；把品牌前缀换掉不等于内化。
7. live event 往往含源码和工具输出。外部 mirror 是新的数据传输权限，不是 observability 的默认功能。

### 19.8 设计边界与剩余差距

当前明确未对齐 InfCode 的部分：

- 没有 HTTP server 与真实 SSE route，也没有连接 abort、socket write failure、CORS/auth 或多 workspace 路由；
- 没有生成 SDK、GUI Bridge、VS Code/JetBrains host 或稳定的客户端版本协商；
- 没有 event schema definition registry/运行时 properties 校验，当前只验证事件名和 object shape；
- A017 完成时没有 per-token message delta、permission/question lifecycle、file watcher、LSP 和 session part 全量事件；
  A019 已补 interaction，A021 已补 text part，其他类型仍缺；
- A017 完成时 replay 只在内存中；A021 已为 HTTP Agent 增加按记录阈值压缩的 JSONL tail、Last-Event-ID 与有限 client 重连；
- subscription 是同步 queue/iterator，没有 async iterator、callback adapter 或跨进程 broker；
- `session.message.completed` 和 `session.tool.completed` 可能携带敏感正文，只适合当前本地信任边界；
- EventBus identity 在一个 Agent 生命周期内绑定；还没有长期 instance service 统一管理多个 run；
- Dodo 目录、server、scheduler、PySide 和兼容测试仍存在，只是退出主架构，尚未物理删除；
- legacy `DodoTraceRecorder` 仍可被显式旧调用方使用，但 core 不会自动实例化；
- 没有为外部 sink 定义 opt-in、字段级脱敏、审计、重试或离线队列协议。

下一步若继续对齐客户端生态，应先实现一个最小的本地 HTTP Session service：由 server route 创建/关闭 subscription，暴露
session/run API 和 SSE `/event`，再在该稳定协议上构建薄 SDK；不应先恢复 Dodo remote mirror。

## 20. A018：本地 HTTP Session service 与薄客户端

### 20.1 InfCode 参考能力与准确边界

本项继续阅读当前工作区中的 InfCode HTTP/Session 实现，主要参考：

- `packages/opencode/src/server/routes/instance/event.ts`：`GET /event`、SSE connected/heartbeat、abort/write failure/finally 清理；
- `packages/opencode/src/server/routes/instance/session.ts`：Session list/get/create/delete/update、status、message、prompt 和 abort 路由；
- `packages/opencode/src/server/routes/instance/httpapi/server.ts`：HTTP route layer、instance middleware、authorization、workspace routing 和 service lifecycle；
- `packages/sdk/js/src/v2/gen/sdk.gen.ts`：由 route contract 生成的 Session client；
- `ARCHITECTURE.md`：CLI backend 通过 HTTP+SSE 服务 SDK、GUI Bridge、VS Code 与 JetBrains host。

InfCode 的完整 server 是多 workspace、多业务域、带 authorization/middleware/生成 SDK 的长期实例服务。A018 没有复制这整套
产品面，只把 A017 的本地事件 primitive 接到一个可以真实访问的进程边界，并保留 CLI 为默认入口。

### 20.2 NZ-Coder 原有状态与实际不足

A017 已经有 `SessionEventBus`、SSE frame 和 Agent 生命周期事件，但仍只是进程内能力：

- `iter_sse()` 没有 socket owner，无法感知真实断开或 write failure；
- 外部脚本必须 import `AgentLoop`，直接依赖 Python 内部类和上下文管理；
- 没有 Session registry、一个 Session 同时运行几次、如何取消和何时 dispose 的服务级规则；
- `nz-coder` 只有 REPL，未来 IDE/App host 没有稳定的本地进程接口；
- 无头 Agent 若遇到 permission `ask`，既有 PermissionManager 会尝试 stdin/`/dev/tty`，可能让 HTTP worker 永久等待；
- 直接使用 `urllib` 时，宿主 `HTTP_PROXY` 可能截获 localhost 请求和 bearer token。

同时必须避免把“增加 HTTP”误解成“现在开始做 App”或“允许远程部署”。第一阶段应是可选本地 backend，而不是 GUI、账号系统
或公网服务。

### 20.3 实现结果

#### 入口与整体调用链

无参数入口仍启动原终端 REPL；只有显式子命令才启动服务：

```text
nz-coder
  -> asyncio.run(_run_cli())

nz-coder serve [--host 127.0.0.1] [--port 4096]
  -> SessionHTTPService
  -> ThreadingHTTPServer
  -> SessionManager
  -> ManagedSession -> AgentLoop
  -> SessionEventBus -> SSE client
```

`interface/cli.py` 将原 async `main()` 收敛为同步 console-script dispatcher，再由 `_run_cli()` 承担原 REPL；这同时修正了
setuptools console script 直接返回 coroutine 的入口问题。`python -m nz_coder` 也会传播真实退出码。

#### 最小 API

| 方法与路径 | 行为 | 成功状态 |
|---|---|---|
| `GET /health` | 无敏感状态的存活检查 | 200 |
| `GET /session` | 列出本进程内 Session | 200 |
| `POST /session` | 创建 Agent，可选 permission mode | 201 |
| `GET /session/{id}` | 状态、消息数、最近结果/错误 | 200 |
| `GET /session/{id}/messages` | 已提交的完整消息历史 | 200 |
| `POST /session/{id}/run` | 接收一条 user message，后台启动 run | 202 |
| `POST /session/{id}/abort` | thread-safe 取消当前 asyncio task | 200 |
| `DELETE /session/{id}` | idle 后 dispose Agent/EventBus 并移出 registry | 200 |
| `GET /event?session_id={id}` | connected、最多 256 条 replay、live event、heartbeat | 200 SSE |

JSON request 必须是 object，拒绝未知字段、非法 UTF-8、错误 Content-Length 和超过 1 MiB 的 body。错误统一为
`{"error":{"code":"...","message":"..."}}`，分别使用 400/401/403/404/409/500。

#### Session manager 与并发生命周期

每个 `ManagedSession` 拥有一个 Agent、一份 committed history 和最多一个 run thread。`SessionManager` 另有一个 service-wide
run gate：因为当前服务的所有 Session 都绑定启动目录，而且每个 AgentLoop 各自持有 transaction manager，若跨 Session 并发写
同一 workspace，一方 rollback 可能覆盖另一方结果。因此第一阶段一次只接受一个 Agent run；同 Session 或其他 Session 的并发
`POST /run` 都返回 409。HTTP 请求仍立即返回 202，客户端可同时连接 SSE；idle Session 的状态读取不受影响。

Agent 执行期间修改私有 `run_messages`；完成后一次性替换 committed history，因此并发 `GET /messages` 不会遍历正在变化的 list。
每次结束 best-effort 调用既有 `save_session(..., activate=False)`。AgentLoop 发出的 `session.run.completed/failed/cancelled` 只代表
Agent 执行终止；manager 在提交 history/status、尝试 persistence 并释放 workspace gate 后，额外发布 `session.run.settled`。
客户端要把 settled 当作 HTTP 层提交屏障，收到后才能保证后续 status 和 run 可用。abort 在 task 尚未创建时也会记录 pending
cancel，worker 建立 task 后立即执行；正常、失败和 cancelled 状态统一清理 loop/task/thread 引用。Server shutdown 先 cancel，
再在总 deadline 内等待，最后 force dispose 所有 EventBus。

同 workspace 的多个 AgentLoop 会共享默认 `MemoryManager`。为避免并行 HTTP Session 在 `memories` cache、Markdown index 或注入
backend 上竞态，本项给 MemoryManager 公共读写入口增加实例级 `RLock`；内部 save/recall/cleanup 的嵌套调用依靠可重入语义，
不同 MemoryManager/workspace 仍可并行。该锁只覆盖同进程实例，不伪称解决多进程文件并发。

#### SSE 的真实 transport owner

`GET /event` 根据 bearer-authenticated `session_id` 创建该 Agent bus 的 bounded subscription。查询可设置 `replay=0..256` 和
逗号分隔的 `types` filter。Handler 写出 `server.connected`、回放事件、live event 和 heartbeat；BrokenPipe、ConnectionReset、
timeout 或 bus dispose 都会进入 `finally` unsubscribe，并关闭 HTTP connection。该 stream 同时传递 Agent execution events 和
manager-owned `session.run.settled`；二者不可互换。

这是单 Session stream，不是 InfCode instance-wide wildcard stream。选择 query 参数而不是全局 bus，可以保持 A017 的 per-Agent
identity 与 sequence，不引入第二套跨 Session fan-out。

#### 本地安全边界

- server 构造器只接受 `127.0.0.1` 或 `localhost`，拒绝 `0.0.0.0` 和远程地址；
- `/health` 之外都要求至少 16 字符的 Bearer token；未设置 `NZ_HTTP_TOKEN` 时启动器用 `secrets` 生成并打印随机 token；
- 这一阶段不是浏览器 API；除无敏感数据的 `/health` 外，authenticated route 带 `Origin` 时直接 403，不开放 CORS；
- bundled client 使用 `ProxyHandler({})` 绕过 `HTTP_PROXY/HTTPS_PROXY`，避免 bearer token 和 localhost 流量进入代理；
- HTTP Agent 注入 `permission_asker`，所有需要人工回答的 `ask` 默认返回 false，不读取 stdin 或 `/dev/tty`；
- `plan/default/acceptEdits/auto` 的既有 allow/deny 规则不变，调用方必须在创建 Session 时明确选择更高权限模式。

#### 标准库薄客户端

`NZCoderClient` 使用 `urllib` 实现 health、list/create/get/delete、messages、run、abort 和 SSE iterator；`NZCoderHTTPError` 保留
status/code/message。它不是生成 SDK，没有重试、类型模型、async API 或自动重连，但已经让脚本不必 import AgentLoop 内部接口。

### 20.4 关键设计决策

#### 标准库 server，而不是引入 Web 框架

项目约束不新增框架依赖。第一阶段只有少量固定 JSON route 和 SSE，`ThreadingHTTPServer` 足以验证 protocol、生命周期和安全
边界。代价是没有 OpenAPI middleware、ASGI cancellation、成熟 router 和 production hardening，因此严格限制在 loopback。

#### 202 异步 run，而不是让 POST 等到模型结束

同步 POST 会让调用方必须在一个长请求中等待，也无法自然地先得到 Session 状态再通过 SSE 观察。后台 task + service-wide
workspace active-run gate 让 transport 和 Agent 生命周期分开，同时避免独立 transaction manager 并发写/rollback 同一目录。
Agent execution 结束与 manager commit 分成两层事件，`session.run.settled` 才是客户端可重新 run 的屏障。

#### Bearer token 即使在 localhost 也必需

localhost 不是天然可信边界：同机其他进程、恶意网页和代理环境都可能访问端口。token、Origin reject、proxy bypass 和 loopback
bind 必须一起存在；只做其中一个不能称为安全的本地代码执行接口。

#### HTTP ask 默认拒绝，不模拟“自动批准”

当前还没有 permission asked/replied event 和 reply endpoint。把 ask 静默升级为 allow 会改变 default mode 的安全语义；尝试
读终端又会挂住 worker。因此 callback 注入返回 false 是可证明的保守降级，后续应通过事件和显式 reply 补齐，而不是绕过。

#### 不是 App，也不强迫 CLI 经 HTTP

REPL 仍直接调用 AgentLoop，启动速度和现有交互不依赖 server。HTTP 是未来 SDK/IDE/App 的可选 backend；在协议和 permission
flow 稳定前，不把 CLI 强制改成自己的网络客户端。

### 20.5 关键文件

- `nz_coder/http_service/manager.py`：Agent factory、Session registry、history、run thread/loop、cancel、persist 和 dispose；
- `nz_coder/http_service/server.py`：loopback server、route、Bearer/Origin/body validation、SSE 和 shutdown；
- `nz_coder/http_service/client.py`：proxy-free 标准库 client、结构化 HTTP error 和 SSE iterator；
- `nz_coder/http_service/cli.py`：serve 参数、API key 检查、token 生成/展示和生命周期；
- `nz_coder/http_service/__init__.py`：公开 client/service/manager API；
- `nz_coder/interface/cli.py`、`nz_coder/__main__.py`：REPL/serve 分发和退出码；
- `nz_coder/tool_platform/permissioning/manager.py`、`nz_coder/runtime/loop.py`：可注入的 permission asker；
- `nz_coder/state/memory.py`：共享 workspace MemoryManager 的实例级并发锁；
- `tests/test_http_service.py`：真实随机 loopback socket、auth、Origin、CRUD、workspace run gate、settled commit barrier、SSE replay、abort、delete 和 CLI dispatch；
- `tests/test_permissions.py`、`tests/test_memory.py`：无头审批 callback 与共享 memory mutation 串行化；
- `README.md`、`.env.example`、`docs/architecture.md`：使用方式、配置和准确产品边界。

### 20.6 验证结果

完成本项时：

- A018 Python 文件严格 Ruff：`All checks passed!`；
- 新增/修改入口和 HTTP 模块 `py_compile` 通过；
- HTTP/Permission/Event/CLI/Loop/Memory/Dodo 聚焦回归：`86 passed`；
- 完整测试套件：`595 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 集成测试真实启动 `127.0.0.1:0` 随机端口，以 HTTP socket 验证 JSON 和 SSE，但使用 fake Agent，不调用模型 API；
- 测试环境实际设置了 `HTTP_PROXY/HTTPS_PROXY`，最初 localhost 请求被代理后 timeout；增加 proxy-free opener 后链路通过，验证了
  该安全边界不是理论假设；
- 非 loopback host、短/错误 token、authenticated browser Origin、非法 permission mode、并发 run/delete 都有拒绝测试，另验证
  无敏感状态的 `/health` 允许普通 Origin probe；
- 按用户要求，没有运行 SWE-bench 官方评测，也没有创建 GUI/App 或远程监听。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/http_service nz_coder/interface/cli.py nz_coder/__main__.py \
  nz_coder/runtime/loop.py nz_coder/tool_platform/permissioning/manager.py \
  nz_coder/state/memory.py tests/test_http_service.py tests/test_permissions.py \
  tests/test_memory.py
python3 -m py_compile \
  nz_coder/http_service/__init__.py nz_coder/http_service/manager.py \
  nz_coder/http_service/client.py nz_coder/http_service/server.py \
  nz_coder/http_service/cli.py nz_coder/interface/cli.py nz_coder/__main__.py \
  nz_coder/state/memory.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_session_events.py \
  tests/test_cli_commands.py tests/test_loop_fake.py tests/test_memory.py \
  tests/test_dodo_integration.py
python3 -m pytest -q
```

### 20.7 学习重点

1. HTTP service 是 Agent core 的可选进程边界，不等于 GUI/App，也不要求废弃 CLI。
2. EventBus 有 framing 仍不等于 SSE 服务；socket owner 必须负责断开、write error、unsubscribe 和 shutdown。
3. active-run cardinality 必须按真实资源边界定义；Session history 虽彼此独立，但共享 workspace 的 transaction/rollback 要求
   第一阶段在 service 层串行运行。
4. 长运行应返回 202 并通过 status/event 观察，而不是占住创建请求直到模型完成。
5. localhost 仍需 token；browser Origin 和环境 proxy 是两个容易漏掉的真实入口。
6. 无头权限的正确默认是拒绝 ask，而不是自动批准或从隐藏终端读取输入。
7. 并发可读状态应读 committed snapshot，不能直接遍历 Agent 正在追加的 message list。
8. Agent 的 terminal event 不自动等于 transport manager 已提交；协议需要 `settled` 这样的明确 commit barrier。
9. 多 Session 隔离不仅是 ContextVar；共享 workspace、transaction、长期记忆 cache/backend 都需明确并发 contract。
10. 标准库足以验证最小协议，但 loopback 限制也是实现成熟度的真实声明，不应宣传为 production server。

### 20.8 设计边界与剩余差距

当前明确未对齐 InfCode 的部分：

- Session registry 只在内存中，server 重启后不会从已保存 JSON 自动恢复或列出历史 Session；
- 一次进程只服务启动目录对应的 workspace，没有 InfCode 的 instance/workspace routing 和隔离 store；
- `/event` 必须指定 `session_id`，不是 instance-wide wildcard bus；A021 已补有界 Last-Event-ID/cursor/reconnect 第一阶段；
- A018 完成时还没有 permission/question asked/replied 事件和 pending registry；A019 已补 interaction，A021 已补 text part，
  todo/file/LSP watcher 和其他 Part 仍未实现；
- A018 完成时 HTTP ask 只能保守拒绝；A019 已增加 Session-scoped reply/reject endpoint、timeout 和 abort 解锁；
- 没有 OpenAPI schema、生成 SDK、async client、版本协商、retry/backoff 或分页；
- 没有 GUI、GUI Bridge、VS Code/JetBrains host，CLI 也尚未通过 HTTP 复用 client；
- 没有 TLS、remote bind、多用户 auth、token rotation/存储、rate limit、审计日志或 CSRF/CORS 浏览器策略；
- 当前服务为保护共享 workspace 而串行化所有 Session 的 Agent run，吞吐量低；未来需要 workspace 隔离或基于 effect/transaction
  ownership 的安全 scheduler，不能直接移除 run gate；
- asyncio cancellation 不能保证立即停止已经进入不可取消阻塞调用的第三方 client/tool thread；
- 运行中 `/messages` 会包含本轮已接受的 user message，但尚未 settle 的 run-local assistant/tool update 不在其中；A021 的增量
  text part 通过 event protocol/journal 提供；A022 已增加持久 message/text-part metadata 与 idle `/snapshot`，但运行中 projection 仍未实现；
- Session delete 只移出本进程 registry 并 dispose Agent，不删除 `.nz-coder/sessions` 的历史文件，语义弱于 InfCode permanent delete；
- MemoryManager 的锁只保护同一进程内共享实例；多个 NZ-Coder 进程同时写同一 memory 目录仍没有文件锁/数据库事务协调；
- 启动器会在终端打印生成 token，尚无 keyring、Unix domain socket 或一次性 capability handoff；
- server 使用标准库实现，尚未做 HTTP parser fuzz、慢请求防护、资源配额或跨平台长时间压力测试。

该项后续已由 A019 补上 permission/question 的第一阶段交互闭环；视觉 GUI 和远程监听仍不在当前范围。

## 21. A019：HTTP permission/question 交互闭环

### 21.1 InfCode 参考能力

本项直接核对当前本地 InfCode，而不是只按 A018 的计划描述实现：

- `packages/opencode/src/permission/index.ts`：pending map、deferred wait、`permission.asked/replied`，回复为
  `once/always/reject`；
- `packages/opencode/src/question/index.ts`：结构化 question request、answers、reply/reject 和
  `question.asked/replied/rejected`；
- `packages/opencode/src/server/routes/instance/permission.ts`：pending list 与 `/:requestID/reply`；
- `packages/opencode/src/server/routes/instance/question.ts`：pending list、reply 与 reject route。

InfCode 的核心不是“多两个 POST”，而是把同步等待用户决策的 Agent 与异步客户端解耦：request 先注册并发布事件，Agent 等待 deferred；
客户端回复后先发布终态事件，再解除 deferred；无论正常回复、拒绝还是异常退出，pending 都必须清理。

### 21.2 NZ-Coder 原有不足

A010 已有结构化 `question` 工具，PermissionManager 也支持注入同步 asker；A018 为避免 HTTP worker 读取 `/dev/tty`，只能给 permission
注入永远 false，question 没有 HTTP adapter。这会造成：

- `default` 模式下写操作只能被拒绝，IDE 无法安全批准一次；
- Agent 发出真正需要用户选择的问题时，只会得到“interactive service unavailable”；
- 没有 pending request ID，SSE 事件与 reply 无法关联；
- abort 若只 cancel asyncio task，而 task 正阻塞在同步回调中，取消回调无法得到执行；
- 客户端断线、回复超时、重复/迟到回复的语义没有定义。

### 21.3 实现结果

#### 调用链

```text
AgentLoop / PlanModeController
  -> PermissionManager.ask_user() or question asker
  -> InteractionBroker atomically registers pending + publishes asked
  -> Agent worker waits on threading.Event

authenticated HTTP client
  -> list pending or consume SSE request ID
  -> reply / reject route
  -> broker removes pending and publishes terminal event
  -> event.set() wakes Agent
  -> Agent continues or receives conservative rejection
```

#### Session-scoped API

| 方法与路径 | 行为 |
|---|---|
| `GET /session/{id}/permission` | 列出该 Session 未决权限请求 |
| `POST /session/{id}/permission/{request}/reply` | `once/always/reject`，可带审计 message |
| `GET /session/{id}/question` | 列出该 Session 未决结构化问题 |
| `POST /session/{id}/question/{request}/reply` | 提交与 questions 等长的 `answers: list[list[str]]` |
| `POST /session/{id}/question/{request}/reject` | 明确 dismiss，不伪造答案 |

未知、已经处理或迟到的 request ID 返回 404；非法 permission reply、answers 数量/类型和单选多值返回 400，且原 pending 保持可回复。
`NZCoderClient` 为以上 route 提供 `pending_permissions()`、`reply_permission()`、`pending_questions()`、`reply_question()` 和
`reject_question()`。pending 注册和 asked 发布由同一个 broker lock 覆盖，因此 list/reply/cancel 不可能观察到“已经可回复但 asked 尚未
发布”的半状态。

#### Permission 语义

- `once`：仅批准当前调用；
- `reject`：当前工具得到 `Denied by user`；
- `always`：只向当前 Agent 的 PermissionManager 添加内存 allow rule，不修改 `.nz-coder/settings.json`；
- Bash 的 always 使用当前规则系统能表达的首命令 prefix，例如 `git `；其他工具使用 tool-name rule；
- 既有 deny、dangerous command 和 Plan mode 硬限制仍先于 allow rule，HTTP reply 不能绕过硬拒绝。

#### 等待、超时和取消

默认交互超时为 300 秒，可通过 `nz-coder serve --interaction-timeout` 修改。timeout 将 permission 解析为 reject，将 question 解析为
dismiss，并分别发布带 `reason=timeout` 的终态事件。SSE 断开不会改变决策：客户端可以重连并重新 list pending；若不恢复，则等待
timeout、显式 abort 或 server shutdown。

Broker 维护 per-run accepting gate。`start_run` 开放新请求；abort 先关闭 gate、拒绝现有 pending，再向 asyncio loop 投递 cancel；
settle/dispose 同样关闭 gate。这样即使 Agent 在 abort 与 callback 注册之间竞态，迟到的 asker 也会立即得到保守拒绝，不会重新挂住
worker。若同步回调在 cancel 后正常返回而 task 来不及观察 `CancelledError`，manager 仍以 `_cancel_requested` 将 settled 状态收口为
cancelled。Manager 另以 `starting/running/committing/idle` 区分内部阶段：只有 task 尚未建立或仍在执行时 abort 返回 true；Agent 已结束、
只剩 persistence/commit 时返回 false，避免声称取消一个已经不存在的 task。

### 21.4 关键设计决策

#### 使用 threading.Event，而不是强迫现有工具 async 化

PermissionManager、question handler 和 PlanModeController 的既有接口都是同步函数。把整个工具注册/dispatch 协议改成 async 会扩大改动
并破坏兼容性。Broker 只阻塞本来就独占一个 run thread 的 Agent，不阻塞 ThreadingHTTPServer 的 reply handler；HTTP handler 可以在
另一个线程原子解析 pending 并唤醒 Agent。

#### 先发布 replied/rejected，再唤醒 Agent

若注册后释放 lock 再发 asked，极快 reply 会产生 `replied -> asked`；若先 `event.set()`，Agent worker 又可能抢先发布后续 message/tool
event。本实现以 broker lock 原子完成 register+asked，也原子完成 pending removal+terminal publish，最后才 set event；timeout 若发现
request 已由其他线程接管，还会等待该 event。A019 的两个可控屏障测试分别锁定 asked 和 terminal 两端的完整因果顺序。

#### 保持 Session-scoped，而不是复制 InfCode instance-global route

A018 还没有多 workspace InstanceState 和全局 wildcard bus。将 pending 挂在 `ManagedSession` 能复用现有 Bearer、Session ownership 和
EventBus identity，不新增一个容易串 Session 的全局 mutable registry。代价是客户端必须知道 session ID，后续做 instance routing 时
再提供聚合视图。

#### 断线不等于 reject

SSE 只是观察连接，可能因网络抖动、客户端重启或读取超时断开。把 transport disconnect 当作用户拒绝会改变业务决策；pending 生命周期
由 reply/reject、abort/dispose 和 timeout 控制，重连后仍可恢复操作。

### 21.5 关键文件

- `nz_coder/http_service/interactions.py`：pending registry、wait/reply/reject、validation、timeout、run gate 和事件顺序；
- `nz_coder/http_service/manager.py`：broker ownership、Agent callback binding、abort/settle/dispose 清理；
- `nz_coder/http_service/server.py`：Session-scoped permission/question HTTP route 和错误映射；
- `nz_coder/http_service/client.py`：pending list 与 reply/reject client 方法；
- `nz_coder/tool_platform/permissioning/manager.py`：可替换 asker 和 once/always/reject 解释；
- `nz_coder/runtime/loop.py`：构造后统一绑定 permission/question/Plan interaction adapters；
- `tests/test_http_service.py`：真实 socket 下的 reply、reject、timeout、迟到回复、因果顺序和 abort-registration race；
- `tests/test_permissions.py`：always session rule 与 Bash prefix 边界。

### 21.6 验证结果

- Ruff：A019 Python 文件 `All checks passed!`；
- `py_compile`：interaction、HTTP、Loop 和 PermissionManager 通过；
- 聚焦回归：`121 passed`，覆盖 HTTP/Permission/Question/Plan/Event/CLI/Loop/Memory/Dodo；
- 完整回归：`605 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 集成测试使用真实 `127.0.0.1:0` HTTP/SSE socket 与 blocking fake Agent，不调用模型 API；
- timeout 测试使用 50ms 配置，验证自动 reject、pending 清空和 run 释放；
- abort race 测试让 Agent 在 event loop 内同步阻塞、先 abort、后尝试注册 question，验证不会产生新的 pending；
- delayed persistence 测试验证 commit barrier 期间 status 仍为 running，但 abort 返回 false，最终保持 completed；
- 按用户要求没有运行 SWE-bench 官方评测，没有创建 App/GUI 或远程 listener。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/http_service nz_coder/runtime/loop.py \
  nz_coder/tool_platform/permissioning/manager.py \
  tests/test_http_service.py tests/test_permissions.py
python3 -m py_compile \
  nz_coder/http_service/__init__.py nz_coder/http_service/interactions.py \
  nz_coder/http_service/manager.py nz_coder/http_service/server.py \
  nz_coder/http_service/client.py nz_coder/http_service/cli.py \
  nz_coder/runtime/loop.py nz_coder/tool_platform/permissioning/manager.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_question.py \
  tests/test_plan_mode.py tests/test_session_events.py tests/test_cli_commands.py \
  tests/test_loop_fake.py tests/test_memory.py tests/test_dodo_integration.py
python3 -m pytest -q
```

### 21.7 学习重点

1. “让 HTTP 能回复”本质是 deferred lifecycle，不只是新增 route。
2. pending 注册与 asked 发布必须相对 list/reply/cancel 原子化，否则会出现 replied 早于 asked 的半状态。
3. replied/rejected 必须先发布再唤醒 Agent，才能保持事件因果顺序。
4. abort 必须同时处理“已经 pending”和“abort 后才尝试 pending”两类竞态。
5. SSE connection 与 interaction ownership 是两回事，断线不应偷偷变成拒绝。
6. always 必须服从既有 deny/Plan/dangerous hard boundary，而且第一阶段只应是 Session 内存规则。
7. validation 失败不能消费 pending，否则用户一次格式错误就无法修正回复。
8. 同步 adapter 可以安全桥接异步客户端，前提是等待发生在专属 worker，并有 timeout/cancel/close 三条退出路径。

### 21.8 设计边界与剩余差距

- pending 只在内存中；server 重启不会恢复卡片，shutdown 会保守 reject 并释放 worker；
- route 是 Session-scoped，没有 InfCode instance-global permission/question list；
- request 没有 InfCode 的 messageID/callID/patterns/tool metadata 完整模型；
- permission reject 的可选 message 当前进入 replied event 供审计，但尚未作为纠正反馈注入模型；
- always rule 受现有规则语法限制：Bash 到首命令 prefix，其他工具到 tool-name；没有 InfCode pattern 集、always-rules 保存接口；
- question reply 没有 InfCode 的 `source`（plan confirm button/user input）和 blocking/non-blocking card 区分；
- 标准库 client 没有自动事件 dispatcher、UI renderer、自动重连或 pending 恢复策略；
- A019 完成时一个 service 只有全局 workspace run gate；A020 已改为每个授权 workspace 独立 gate，不同 workspace 可并行；
- 没有浏览器 CORS/CSRF contract、GUI Bridge 或 IDE host，仍是 loopback script backend。

## 22. A020：HTTP workspace routing 与持久恢复

### 22.1 InfCode 参考能力

本项重新核对当前本地 InfCode 的 workspace/session 路由，而不是把 HTTP route 简单加一个路径参数：

- `packages/opencode/src/server/routes/instance/httpapi/middleware/workspace-routing.ts`：结合 Session 的
  `workspaceID`、显式 workspace 选择和默认 directory 生成本地/远程 request plan；
- `packages/opencode/src/server/workspace.ts`：优先按已保存 Session 的 workspace identity 路由，并在对应
  Instance context 中执行请求；
- `packages/opencode/src/project/instance-context.ts` 与 `project/instance-store.ts`：按 directory/workspace
  隔离 instance service 与状态；
- `packages/opencode/src/session/session.ts`：把 `workspaceID` 作为持久 Session identity 的一部分，而不是每次
  run 临时提供的参数。

InfCode 的完整实现还包含数据库、local/remote workspace、代理、同步 fence 和 disposal。NZ-Coder 第一阶段只对齐
本地多 workspace 的关键不变量：**workspace 必须先被服务拥有，Session 一旦创建就固定归属，之后所有请求都按
Session 身份路由。**

### 22.2 NZ-Coder 原有不足

A014 已用 `ContextVar` 隔离 Agent workspace，A018/A019 也已有 HTTP Session 和交互闭环，但 HTTP manager 仍只有
一个进程级 run gate，且只知道启动目录：

- HTTP client 无法在多个项目间选择 workspace；
- 同一 service 下任意两个 Session 都互相阻塞，即使它们属于完全不同的目录；
- manager 重启后内存 Session registry 清空，已经保存的 history 只能回到 CLI 手工恢复；
- 若直接开放绝对路径参数，拿到 Bearer token 的客户端就能任意改变 Agent 工作目录；
- manager 持久化发生在 `AgentLoop.run()` 的 workspace scope 退出之后，未来多 workspace 下会有保存到错误目录的风险。

### 22.3 实现结果

#### 启动者登记与稳定 workspace ID

```text
service operator
  -> nz-coder serve [--workspace PATH ...]
  -> WorkspaceRegistry resolve + existence/directory validation
  -> stable ws-<sha256-prefix> identity

authenticated client
  -> GET /workspace
  -> POST /session {workspace_id, permission_mode}
  -> live Session binds workspace_id + resolved path
  -> disk payload persists resolved path; restart maps it to the current ID
```

当前启动目录始终是默认登记 root；`--workspace` 可重复添加额外 root。所有 root 先 `resolve()`；重复路径去重，父子
嵌套或其他可由解析后路径识别的重叠关系会拒绝启动，避免**可被该检查识别的重叠 root**使用不同 lock 域。HTTP create/run JSON 不接受路径，
只接受 registry 生成的 `workspace_id`。未知 ID 返回 404 `workspace_not_found`，`POST /session` 也拒绝未声明字段，因此
客户端不能用 `../`、绝对路径或 symlink 文本临时改变 cwd。`GET /workspace` 会向已认证客户端返回真实 path：ID 是
稳定选择器，不是秘密 capability，也不用于隐藏目录。

`GET /workspace` 返回 `id/path/default`；`NZCoderClient.list_workspaces()` 和兼容扩展后的
`create_session(permission_mode=None, workspace_id=None)` 提供同一能力。不传 ID 时继续使用默认 workspace，旧调用方
不需要修改。

#### Session 固定路由与并发边界

```text
SessionManager.create(workspace_id)
  -> scoped_workdir(registered path)
  -> construct AgentLoop (captures its workdir)
  -> ManagedSession stores workspace identity

run / messages / permission / question / events / abort
  -> resolve Session ID
  -> use the Session-owned workspace
  -> never accept a per-request path override
```

原来的 service-wide lock 改成 `workspace_id -> threading.Lock`。同 workspace 两个 Session 仍只能有一个 active run，
第二个 run 不等待锁而是立即返回 HTTP 409 `session_busy`。permission/question list/reply、events、abort 等控制请求不获取
run gate，因此执行期间仍可响应。不同且不重叠的 workspace 使用不同 lock，在 HTTP manager/路由层允许并发；这不等于
provider client、操作系统资源或所有外部服务已经实现进程级完全隔离。manager 自己持久化 history 时重新进入该 Session 的
`scoped_workdir`，不能依赖 Agent 内部 scope 尚未退出。若要切换 workspace，客户端必须创建新 Session，不能修改既有归属。

HTTP Agent 的 system prompt 也必须在同一 scope 内构造。原实现直接读取模块级 `memory_mgr` 和 `skill_loader`；若只修
AgentLoop 的 `workdir`，额外 workspace 仍会把默认项目的 memory/skill 描述注入 prompt。现在 builder 按目标目录选择或新建
`MemoryManager`/`SkillLoader`，在构造 AgentLoop 时绑定同一实例，从 prompt 到工具执行都保持同一 workspace identity。

#### 重启发现与懒恢复

服务启动时只扫描登记 root 下的 `.nz-coder/sessions/*.json`，每个 root 最多检查 1000 条最近文件，单文件上限
16 MiB。每条候选必须同时满足：

1. 文件名、payload `session_id` 完全一致且只含安全字符；
2. `messages` 是由对象组成的列表；
3. permission mode 是当前支持值；
4. payload 显式保存的绝对 workspace path 解析后与当前登记 root 完全相等，再由当前 registry 映射成 workspace ID；
5. 同一 Session ID 不在两个 workspace 中冲突。

合法记录先进入轻量 `dormant` registry，`GET /session` 能看到 workspace、mode 和 message count，但不会在启动风暴中
一次性构造所有 Agent。第一次访问详情、messages、events 或继续 run 时，manager 才在正确 workspace scope 下构造 Agent，
深拷贝旧 history 并切换为 live `idle` Session。继续 run 后，新 user/assistant 消息仍保存回原 workspace。

DELETE 只移除当前进程 registry，与 A018 的既有语义一致，不悄悄删除学习记录或会话文件；因此文件仍存在时，下次
服务重启可再次发现。损坏、缺少 workspace、跨 workspace 伪造或重复 identity 的记录会被安静跳过，不阻断其他恢复。

### 22.4 关键设计决策

#### 不允许 Bearer client 直接提交目录

loopback 和 Bearer token 解决“谁能调用 API”，并不自动解决“他能把 Agent 的 cwd 设到哪里”。由启动者通过
`--workspace` 登记，再让客户端选择 ID，限制的是 **HTTP 控制面可选择的工作目录**，不是 OS 文件系统沙箱。内置文件工具
仍有 `_safe_path()` workspace 检查，但 Bash/子进程仍拥有服务账户的系统权限，可能访问 root 之外。需要强隔离时必须使用
最小权限账户、容器或外部 sandbox，不能把 workspace registry 当成 chroot。

ID 是 resolved path 的 SHA-256 前 16 个十六进制字符，并以 `ws-` 开头；注册时若出现 ID 碰撞会拒绝启动。它只避免
create/run 接口接受任意路径，不承担保密作用。

#### Session workspace 是 identity，不是 run option

若每次 run 都能覆盖 workspace，同一 history、permission always rule、event stream 和 artifacts 会突然对应另一个项目，
也会制造检查后切换目录的竞态。固定归属使所有 Session-scoped route 都能只用 Session ID 安全路由。

#### 懒恢复 Agent，而不是启动时全部实例化

历史文件可能很多；真实 Agent 构造会加载 prompt、memory、skills 和 provider 状态。启动只做数量与单文件大小有界的元数据扫描，既保留
可发现性，又避免旧会话数量线性放大服务启动开销。构造失败时 saved descriptor 仍保留，后续可修复环境后重试。

#### 没有复制 InfCode 的远程 workspace 层

NZ-Coder 当前是 loopback-only 本地后端，没有 workspace control plane、同步协议或可信 remote endpoint。第一阶段引入
proxy/fence 会增加未经验证的网络和一致性语义。当前 registry 是 InfCode instance routing 的本地最小子集，不伪称已经
对齐其 local/remote 完整架构。

### 22.5 关键文件

- `nz_coder/http_service/workspaces.py`：登记 root 解析、重叠拒绝、稳定 workspace ID、反向查找和 JSON 描述；
- `nz_coder/http_service/manager.py`：Session workspace ownership、per-workspace gate、workspace prompt state、持久扫描、校验和懒恢复；
- `nz_coder/http_service/server.py`：`GET /workspace`、create workspace 选择和 404 错误映射；
- `nz_coder/http_service/client.py`：workspace list 与向后兼容的 create 参数；
- `nz_coder/http_service/cli.py`：可重复 `--workspace` 启动者登记；
- `tests/test_http_service.py`：授权边界、不同 workspace 并行、重启继续 history、损坏/跨目录记录拒绝和 CLI 透传；
- `README.md` 与 `docs/architecture.md`：用户入口、调用链、安全边界和仍未实现范围。

### 22.6 验证结果

- Ruff：`nz_coder/http_service` 与 `tests/test_http_service.py` 为 `All checks passed!`；
- `py_compile`：workspace、manager、server、client 和 CLI 模块通过；
- HTTP 专项：`21 passed`；
- 聚焦回归：`128 passed`，覆盖 HTTP/Permission/Question/Plan/Event/CLI/Loop/Memory/Dodo；
- 完整回归：`612 passed`，只有 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- `test_http_different_workspaces_run_concurrently` 通过真实 loopback socket 在两个临时 workspace 同时接受 blocking run，
  再分别 abort，证明不同 workspace lock 在 HTTP manager 层不互相阻塞；
- 重启测试先在临时 workspace 完成并持久化两条消息，再创建新 manager，验证 list 为 dormant、Agent 尚未构造、继续 run 后
  history 增长为四条且仍写回同一目录；
- `test_http_workspace_registry_and_unknown_workspace` 与 `test_workspace_registry_rejects_invalid_roots` 覆盖未知/畸形 ID、
  不存在 root、文件伪装 root 和嵌套 root；
- `test_http_restart_discovers_and_lazily_restores_session`、`test_http_restore_skips_corrupt_or_cross_workspace_metadata` 和
  `test_http_restore_skips_oversized_session_file` 覆盖 dormant 继续、workspace 伪造、损坏 JSON 与 16 MiB 上限；
- `test_http_agent_prompt_uses_the_selected_workspace_state` 验证 HTTP builder 选择目标 workspace 的 memory manager、
  project skill 路径和 skill description，没有退回模块级默认状态；
- 验证环境快照：Python `3.13.12`、pytest `9.0.3`、Ruff `0.15.10`、Linux
  `7.0.0-28-generic x86_64`；测试数量是本项完成时的共享工作区快照，后续会增长；
- 本轮遵循项目当前工作方式，没有创建 Git commit，也不以 commit 作为运行前提；因此这些数量不能在未来仅靠 commit ID
  精确复现。要冻结面试/发布证据，应另行归档当时的完整源码、依赖锁定和命令输出；
- 按用户要求没有运行 SWE-bench 官方评测，没有创建 GUI/App、远程 listener 或 workspace proxy。

复现命令：

```bash
python3 -m ruff check nz_coder/http_service tests/test_http_service.py
python3 -m py_compile \
  nz_coder/http_service/workspaces.py nz_coder/http_service/manager.py \
  nz_coder/http_service/server.py nz_coder/http_service/client.py \
  nz_coder/http_service/cli.py
python3 -m pytest -q tests/test_http_service.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_question.py \
  tests/test_plan_mode.py tests/test_session_events.py tests/test_cli_commands.py \
  tests/test_loop_fake.py tests/test_memory.py tests/test_dodo_integration.py
python3 -m pytest -q
```

### 22.7 学习重点

1. 多 workspace HTTP 服务首先要定义控制面能选哪些 cwd；这仍不等于操作系统级 sandbox。
2. workspace 应成为 Session identity；让每个 run 自由换目录会同时破坏状态隔离与安全边界。
3. `ContextVar` 只能在显式 scope 内生效；manager 在 Agent scope 之外保存时必须自己重新绑定 workspace。
4. manager 的并行单位应与已知共享文件边界一致：同 workspace 串行，非重叠 workspace 才允许并行。
5. 持久恢复不是“读 JSON 就运行”，而是身份、shape、权限模式和 workspace ownership 的输入校验链。
6. dormant descriptor 能把“可发现”与“昂贵实例化”分开，是服务重启和资源控制间的简单折中。
7. 稳定 selector ID 避免 create/run 接口接受 cwd；它不是秘密，也不能替代 OS 权限或 sandbox。
8. 对齐 InfCode 不等于复制其远程 control plane，应按项目当前产品边界截取可验证的本地不变量。

### 22.8 设计边界与剩余差距

- Session history 使用文件快照，没有数据库事务、schema migration、分页或跨进程 writer coordination；
- dormant 的 `created_at`/`updated_at` 只能从文件 mtime 恢复，原始创建时间尚未进入持久 schema；
- pending permission/question 仍只在内存中，重启只恢复 committed history，不恢复正在等待的交互或 active run；
- DELETE 不是永久删除 API，服务重启后仍可能重新发现保存文件；尚无 archive/purge contract；
- A020 完成时没有 Last-Event-ID、持久 event cursor、断线自动续传或 message part 增量恢复；A021 已补第一阶段；
- 没有 InfCode 的 remote workspace、proxy、sync fence、control API、instance disposer 或 workspace 级资源配额；
- workspace ID 来自本机 resolved path；目录移动或不同机器不会保持同一 ID，也没有项目级 portable identity；
- resolved path 能识别 symlink 与父子重叠，但 bind mount、硬链接或其他文件系统别名仍可能绕开 root-overlap 检测；
- 启动扫描固定为每 workspace 最近 1000 个、每文件 16 MiB；没有时间预算、总内存配额或可配置分页；
- 不同 workspace 虽可并行，但仍需继续审计 provider client、长期 memory 和可选外部资源的进程级共享状态；
- 仍无 GUI Bridge、VS Code/JetBrains host；HTTP service 继续只是可选本地 backend，不是 App。

## 23. A021：HTTP message part 与 SSE 游标恢复

### 23.1 InfCode 参考能力与事实边界

本项核对了当前本地 InfCode 的 part 生成链和 SSE client，而不是把“SDK 有重连代码”误写成“server 已有完整游标”：

- `packages/opencode/src/session/message-v2.ts`：定义 `message.part.updated`、`message.part.delta` 和
  `message.part.removed`；delta 包含 message ID、part ID、field 与追加文本；
- `packages/opencode/src/session/processor.ts`：首次文本建立 TextPart，每个 provider text delta 调用
  `updatePartDelta()`，完成时更新完整 Part；reasoning/tool 也有各自 part 生命周期；
- `packages/opencode/src/server/routes/instance/event.ts` 与
  `server/routes/instance/httpapi/event.ts`：提供 connected、live event、heartbeat 与断开清理；
- `packages/sdk/js/src/v2/gen/core/serverSentEvents.gen.ts`：解析 SSE `id:`，连接失败时把最新
  `lastEventId` 放进 `Last-Event-ID` header，并做有上限的退避重试。

必须明确：当前本地 InfCode 的两套 event route 在写 SSE 时仍把 `id` 设为 `undefined`，也没有按
`Last-Event-ID` 查找服务端历史。因此 **assistant text part 的 updated/delta/removed schema 与基本生命周期是直接对齐项；
服务端 journal、410 过期语义与跨重启 cursor replay
是 NZ-Coder 为现有本地 EventBus 增加的增强**，不能反向宣称 InfCode server 已具备同样实现。

### 23.2 NZ-Coder 原有不足

A017 事件 envelope 已包含 UUID event ID 和单调 sequence，A018 提供 SSE replay query，A020 又恢复了 Session history；
但这三层没有形成真正的断线协议：

- HTTP manager 固定用 `stream=False`，客户端只能在整轮完成后收到 `session.message.completed`；
- provider 即使按 chunk 返回文本，EventBus 也没有 message/part identity，UI 不能增量拼接或撤销失败的半段；
- `encode_sse()` 只写 `data:`，envelope 中虽有 event ID，标准 SSE client 不会把它当 reconnect cursor；
- replay 只能说“最近 N 条”，不能表达“严格从我处理完成的这条之后开始”；
- cursor 已被截断时若静默返回最近事件，客户端无法知道中间有缺口；
- 标准库 client 断线即结束，没有携带 cursor 的重连策略；
- 服务重启后 Agent EventBus 重建，sequence/replay 全部丢失，即使 A020 已恢复 conversation history 也无法续接事件。

### 23.3 实现结果

#### Text part 生命周期

```text
HTTP manager -> AgentLoop.run(stream=True)
  -> first provider text chunk
     -> message.part.updated(empty TextPart)
     -> message.part.delta(delta="...")
  -> more chunks
     -> message.part.delta(...)
  -> successful model response
     -> message.part.updated(full TextPart + end time)
     -> session.message.completed(same message_id/part_id)
```

每个 assistant model turn 生成稳定的 `msg-<uuid>` 与 `part-<uuid>`。Part 使用 `type=text`、完整 text、start/end time；
delta 使用 `field=text`。非 streaming provider 不伪造 token delta，只在完整响应到达时发布最终 updated。CLI 原有 renderer
仍遵守“工具回合的方案性 preamble 不逐 token 刷屏”的策略：part event 直接发往 EventBus，不复用 `on_token`，因此 HTTP/IDE
可以增量显示而终端体验不回退。

若流在已经发布部分文本后失败，Loop 先发布 `message.part.removed`。可重试错误保留同一 message ID、换一个新 part ID；
客户端先丢弃旧 part，再接收新 attempt。不可重试或中断同样移除半成品，避免 UI 把未提交文本当成最终回答。第一阶段只覆盖
assistant text；reasoning、tool input/output、file、step 等 InfCode Part 类型尚未移植。若失败发生在第一个 text chunk 之前，
尚未创建 part，因此也不会发布 `removed`。

#### 客户端 reducer 契约

Part event 是增量显示协议，不是简单地把字符串都拼起来。宿主应遵守以下最小 reducer 规则：

1. 以 `meta.event_id` 去重；重连、调用方崩溃恢复或半帧重发都可能让同一业务事件再次出现；
2. `message.part.updated` 是完整 snapshot，按 `(message_id, part_id)` upsert/replace，不能追加或字段盲合并；
3. `message.part.delta` 只追加到相同 identity 且尚未 tombstone 的 part；
4. `message.part.removed` 删除并 tombstone 该 part，之后忽略同一 part ID 的迟到 delta/update；retry 会使用新 part ID；
5. 最终 `updated` 仍是 provisional UI state。若 live part 已存在，只有 identity 匹配的 `session.message.completed` 才提交它；
   若从未看到该 identity（例如订阅过滤、队列丢弃或 snapshot 恢复），completed 自带的完整 `content` 可创建并提交最终消息；
   若 identity 已被 `removed` tombstone，则迟到的 completed 不得把它复活。

Loop 中最终 `updated` 与 `completed` 连续同步发布，但 transport drop、进程 crash 或慢订阅者 queue drop 仍可能让客户端只看到前者。
因此 reducer 不能把 full updated 本身当作 commit；重启或 snapshot resync 时应清除所有未 completed 的 provisional part。

HTTP abort 还有额外的线程边界：`asyncio` task cancellation 不能强停已经进入 `run_in_executor()` 的 provider worker。A021 为共享
message attempt 增加锁和 retired flag；取消先把已 started 的 part `removed` 一次，再令 worker 忽略迟到 chunk。取消任务会等待该
worker 真正退出后才发布 `session.run.cancelled/settled` 并释放 workspace run gate，所以新 run 不会和旧 provider 调用重叠。
重复 abort 在第一次请求后返回 false，worker drain 也会吸收重复的 task cancellation，不能绕过该 barrier。若 provider 永久阻塞且
不返回，abort 也会保持在进行中；这是当前无法强制终止第三方线程的安全取舍。

#### 标准 SSE cursor

`encode_sse(SessionEvent)` 现在生成：

```text
id: <event UUID>
data: {"type": "...", "properties": {...}, "meta": {...}}
```

`server.connected` 与 `server.heartbeat` 是 transport control frame，没有 ID，不推进 cursor。HTTP route 读取标准
`Last-Event-ID`，在 Session 的完整 recent tail 中找到该事件，并在持有 EventBus lock 时把“它之后的 retained events”先放进
新 subscription，再开放 live fan-out，防止 replay/live 交界处被并发 publish 插队。event type filter 只影响交付，不影响 cursor
在全局 Session sequence 中的定位。

event ID 的语法为 `[A-Za-z0-9_-]{1,128}`。若 cursor 格式非法则返回 400；格式合法但不属于该 Session、从未存在或已经离开
retained tail 时统一返回 HTTP 410
`event_cursor_expired`。服务不会偷偷回退到 `replay=256`，因为那会把“可能缺事件”伪装成成功恢复。客户端遇到 410 应重新读取
`/messages`、pending permission/question 与 Session status，清除旧 cursor 和所有 provisional part，再从不携带旧 cursor 的连接继续。
若请求同时带 `Last-Event-ID` 与 `replay`，cursor 语义优先，数字 replay 不参与 fallback。

这不是无损恢复协议：多个 GET 与新 subscription 之间没有原子的 snapshot/cursor watermark，期间发生的事件可能重复或遗漏；
`/messages` 也没有 message/part ID，只能重建已提交消息基线，不能无缝续接原来的增量 part。A021 选择明确披露该竞态，后续需要
带 watermark 的 snapshot endpoint 才能真正闭合。

#### 按记录阈值压缩的持久 journal

HTTP Agent 的 EventBus 使用 Session runtime 下的：

```text
.nz-coder/sessions/_artifacts/<session-id>/runtime/events.jsonl
```

每行保存完整 wire envelope。默认内存 replay capacity 为 256；journal 达到 1024 records 时用临时文件、文件 `fsync` 和 replace
压缩为当前 256 条 tail。因此正常文件通常在压缩后的 256 到再次触发前的 1023 条之间；它只按记录数周期压缩，单条 payload
没有 byte cap，不能称为磁盘字节有界。重启最多从文件尾读取 16 MiB；从中间开始时先丢弃可能被截断的首行，再逐行验证 event
type、properties、schema、有限 timestamp、安全 event ID、Session identity 与连续递增 sequence。Loader 只把最后一段连续且有效的
suffix 暴露给 replay：损坏、其他 Session、duplicate、倒序或 sequence gap 都会让此前 prefix 失去 replay 资格，所以落在缺口前的
cursor 返回 410，而不会跨过已检测到的缺口。新事件继续使用扫描到的最大有效 sequence+1，但仍生成新 UUID。A020 materialize
dormant Session 时，HTTP Agent builder 在正确 workspace 中打开该 journal，因而仅当
cursor 仍在加载出的 tail、相关记录成功落盘且通过校验时，才可跨 service restart 重放。

Journal append 是 best-effort：磁盘错误不能让 Agent run 失败；这也意味着“能继续运行”优先于“保证每个 cursor 永久可恢复”。
CLI 自己创建的 EventBus 仍只在内存中，不会因为 A021 自动写 journal。当前 compaction 只 `fsync` 临时文件，没有 `fsync`
父目录；也没有跨进程 writer lock、断电级 durable replace、symlink 专项防护或自动清理机制。若中间 record 因写入失败或损坏消失，
loader 会废弃缺口前的 replay prefix；但未被校验识别的存储故障仍不可能由 JSONL 自证完整，所以 journal 只能用于 best-effort
reconnect，不能作为端到端 delivery guarantee。

#### Client 自动重连

`NZCoderClient.events()` 新增向后兼容参数：

```python
events(
    session_id,
    replay=256,
    event_types=None,
    last_event_id=None,
    reconnect_attempts=0,
    reconnect_delay=0.25,
)
```

默认 `reconnect_attempts=0` 保持旧行为。启用后，client 只在一个完整 SSE frame 已解析、yield，且调用方继续拉取时更新 cursor；
TCP reset、EOF 或解析错误后的下一次请求携带该 cursor。半帧不会推进 ID，因此 server 会重发它。HTTP 4xx/5xx（包括 410）作为结构化
`NZCoderHTTPError` 交给调用者，不在无意义的自动重试中循环。每次 transport 重连都会重新收到无 ID 的 `server.connected`。
这里的“调用方继续拉取”只是 generator 的局部确认，并不证明业务 handler 已持久提交；重复 event 仍由 reducer 按 event ID 去重。

### 23.4 关键设计决策

#### Part event 与终端 token callback 分离

NZ-Coder 过去有意缓存 tool-turn preamble，避免终端重复显示“计划”后马上执行工具。直接恢复每 chunk `on_token()` 会破坏这个行为。
EventBus 是客户端协议，renderer 是终端显示策略；分开后，IDE 能构建实时 Part，CLI 仍只在最终回答时显示一次。

#### Cursor 查不到就 410，不猜测

sequence 连续并不代表服务仍持有中间 payload。客户端最危险的状态不是重连失败，而是误以为没有遗漏。明确 Gone 迫使它走
snapshot/resubscribe 路径，也让 bounded storage contract 可测试。

#### Cursor 只在完整 frame 后推进

连接可能在 `id:`、半行 JSON 或 data frame 结尾之前断开。若收到 `id:` 就立即保存，客户端会跳过尚未成功解析/处理的数据。
当前 parser 只在空行结束完整 frame、成功 JSON decode、交付调用方且迭代继续后更新 cursor。这只形成 parser/transport 局部的
“不因半帧推进游标”边界，不是端到端 at-least-once 保证：它不知道业务侧是否已提交，live queue 会静默 drop，journal 又是
best-effort。调用方必须容忍重复，也不能据此假设无缺口。

#### 持久化 event ID，而不是仅持久 sequence

SSE 标准传递的是字符串 ID；UUID 不暴露“当前有多少事件”，也能精确拒绝另一个 Session 的 cursor。sequence 仍用于本地排序与诊断，
journal 同时保存两者。

#### Journal 不是 pending state database

Permission/question asked/replied event 是历史事实，不是 broker 的 authoritative pending map。服务 crash 后 A019 deferred worker 不存在；
即使 journal 重放一条旧 asked，客户端也必须以 `/permission`、`/question` 当前列表为准。A021 没有伪造 active run 或 pending 恢复。

### 23.5 关键文件

- `nz_coder/session_events.py`：event journal、record 校验、strict-after cursor、过期异常与带 ID SSE framing；
- `nz_coder/runtime/loop.py`：text part IDs、delta/update/remove 生命周期，以及 stream retry part 替换；
- 同一 Loop 模块的 attempt retirement：HTTP cancel 后抑制 provider worker 的迟到 delta，并在 worker 退出前保留 run gate；
- `nz_coder/http_service/manager.py`：HTTP Agent journal 绑定与 streaming run；
- `nz_coder/http_service/server.py`：`Last-Event-ID`、strict replay 和 HTTP 410 映射；
- `nz_coder/http_service/client.py`：SSE frame/ID parser、cursor validation 与有限自动重连；
- `tests/test_session_events.py`：cursor 原子 replay、journal restore/compaction、stream delta 与 retry remove；
- `tests/test_http_service.py`：真实 socket resume、410、跨重启 cursor 和 fake transport 自动重连。

### 23.6 验证结果

- Ruff：A021 源码和测试为 `All checks passed!`；
- `py_compile`：EventBus、Loop、HTTP manager/server/client 通过；
- Event/HTTP 专项：`40 passed`；
- 聚焦回归：`140 passed`，覆盖 HTTP/Permission/Question/Plan/Event/CLI/Loop/Memory/Dodo；
- 完整回归：`624 passed`，观察到 3 个 websockets/uvicorn/multiprocessing fork 告警；
- `test_agent_stream_publishes_incremental_text_part_lifecycle` 验证 empty updated → 两个 delta → full updated → completed；
- `test_stream_retry_removes_partial_part_before_new_attempt` 验证同 message、新 part 和 removed 因果顺序；
- `test_session_event_cursor_replays_strictly_after_and_expires` 验证 cursor/live 订阅原子边界与过期；
- `test_session_event_journal_restores_sequence_and_cursor`、`test_session_event_journal_compacts_to_bounded_tail` 验证重启和 1024/256
  compaction 语义；
- `test_session_event_journal_replays_only_contiguous_suffix_after_gap` 与
  `test_session_event_journal_corruption_invalidates_earlier_cursor` 验证缺失/损坏 record 之前的 cursor 被 410 拒绝；
- `test_http_sse_resumes_strictly_after_last_event_id` 与 `test_http_sse_expired_cursor_returns_gone` 使用真实 loopback SSE；
- `test_http_sse_cursor_survives_service_restart` 验证 A020 dormant materialization 后仍能从旧 event ID 继续；
- `test_http_client_reconnects_with_latest_complete_event_id` 用可控断线 transport 验证只携带最新完整 frame ID；
- `test_http_abort_retires_stream_part_before_run_settles` 用阻塞 provider stream 验证 abort 后 exactly-one remove、迟到 delta 抑制、
  重复 abort 幂等、worker 退出前拒绝新 run，以及 removed → cancelled → settled 顺序；
- 验证环境：Python `3.13.12`、pytest `9.0.3`、Ruff `0.15.10`、Linux `7.0.0-28-generic x86_64`；
- 上述 40/140/624 是逐级重叠的测试集合，不能相加；命令可在当前 checkout 复跑，但未保存独立测试产物或锁定新的 commit；
- 按用户要求没有运行 SWE-bench 官方评测，没有创建 App/GUI、远程 listener 或 event broker。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/session_events.py nz_coder/runtime/loop.py nz_coder/http_service \
  tests/test_session_events.py tests/test_http_service.py
python3 -m py_compile \
  nz_coder/session_events.py nz_coder/runtime/loop.py \
  nz_coder/http_service/manager.py nz_coder/http_service/server.py \
  nz_coder/http_service/client.py
python3 -m pytest -q tests/test_session_events.py tests/test_http_service.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_question.py \
  tests/test_plan_mode.py tests/test_session_events.py tests/test_cli_commands.py \
  tests/test_loop_fake.py tests/test_memory.py tests/test_dodo_integration.py
python3 -m pytest -q
```

### 23.7 学习重点

1. “有 event ID”不等于“有 reconnect protocol”；transport 必须真的写 `id:`，server 也必须解释 `Last-Event-ID`。
2. 增量 UI 需要稳定 message/part identity、delta 和终态 snapshot，不能只把 token 字符串往外推。
3. retry 之前必须 remove 半成品，否则新 attempt 会和旧文本拼在一起。
4. coroutine cancel 不会停止 executor thread；必须 retire shared attempt、屏蔽迟到事件，并在 worker 退出前保留 run gate。
5. cursor replay 的核心是 strict-after 与 replay/live 原子交界，不只是从 deque 切一段。
6. bounded history 必须有显式过期错误；silent fallback 会制造不可检测的数据缺口。
7. cursor 只能在完整 frame 交付、调用方继续迭代后推进；这能避免半帧造成跳过，但不等于业务提交或端到端 at-least-once。
8. event journal、conversation snapshot 和 pending interaction registry 是三种不同真相，不能互相冒充。
9. 对齐源码时要区分 schema、SDK 通用能力和 server 实际 wiring；看到 generated client 的重连代码不能直接推断服务端已有 cursor。

### 23.8 设计边界与剩余差距

- 第一阶段只建 assistant text part；没有 reasoning、tool、file、step、snapshot、patch 或 question Part 完整模型；
- A021 完成时 message/part ID 只属于 event protocol；A022 已把 identity 写入持久 metadata 并新增 structured snapshot，同时故意保持
  `GET /messages` 的既有 role/content/tool_calls 兼容形状；
- journal 只为 `build_http_agent` 创建的 HTTP Agent 启用，CLI EventBus 与任意自定义 factory 默认仍是 memory-only；
- journal 是 best-effort JSONL，没有数据库事务、跨进程 writer lock、父目录 `fsync`、断电级 durable replace、加密、远程复制或
  exactly-once 保证；并发进程 append/compaction 可能覆盖、乱序或损坏 JSONL；
- journal 会复制 event payload，包括模型文本、工具结果和 permission/question metadata；它继承 `.nz-coder` 的本地文件权限，
  尚无字段级 redaction、明确的文件 mode 强化、symlink 专项防护、自动清理或独立 retention policy；低流量敏感记录可能长期保留；
- clean shutdown 会记录历史 `session.disposed`；crash 可能留下没有终态的旧 `permission.asked/question.asked`。重放是审计历史，
  pending GET route 才是当前状态；
- 内存只保留 256 条；journal 达 1024 条时压到 256，加载只看文件尾 16 MiB，但单条记录没有 byte cap，磁盘大小并非严格有界。
  loader 只开放最后连续有效 suffix，检测到缺失/损坏记录会让之前的 cursor 410；仍没有按时间/字节的可配置策略；
- live subscriber 自己的 queue 仍可能因消费过慢丢旧事件，当前没有主动 gap notification；断线后 cursor replay 不能修复客户端未察觉的
  queue drop；
- event filter 不改变全局 cursor 定位，但过滤事件不向客户端交付 ID；长期只收稀疏类型会让其可见 cursor 老化并更容易在重连时 410；
- client 是固定次数、固定 delay 重连，没有 InfCode generated SDK 的指数退避、server `retry:` 支持或随机抖动；
- abort 依赖 provider worker 协作退出；retired flag 能阻止迟到事件和新 run 重叠，但无法强杀永久阻塞的第三方线程；
- A021 完成时 410 后的 messages/status/pending 多次 GET 没有原子 watermark；A022 已为 idle Session 增加 structured snapshot cursor，
  但 running Session 必须先 settle/abort，且 cursor 在订阅前再次过期时仍需重新 snapshot；
- 每次重连都会再次 yield `server.connected`，调用方必须把它当 control frame而非业务事件；
- 当前 InfCode server 自身也没有 wired SSE ID/Last-Event-ID replay；A021 的持久 cursor 是 NZ-Coder 增强而非一对一复制；
- 仍没有多进程/多节点 event broker、SDK 代码生成、GUI Bridge 或 IDE host。

## 24. A022：持久 message identity 与原子 idle snapshot

### 24.1 InfCode 参考能力与事实边界

本项重新阅读了当前本地 InfCode 的持久消息链，而不是把 A021 的 event-only Part 当成完整 MessageV2：

- `packages/opencode/src/session/message-v2.ts`：`WithParts` 明确定义为 `{info, parts}`，message 与 part 都有稳定 ID；
- `packages/opencode/src/session/session.sql.ts`：message info 与 part 分表保存，row identity 与 JSON data 分离；
- `packages/opencode/src/session/session.ts`：`updateMessage()`、`updatePart()`、`removePart()` 通过 SyncEvent 更新持久投影，`messages()`
  返回持久的 `MessageV2.WithParts[]`；
- `message.part.delta` 仍是 live Bus event，而完整 part update/remove 进入可恢复投影。

InfCode 这部分是 **持久 message/part projection 的直接参考**。但当前本地 InfCode event route 仍没有 wired SSE ID，因此 A022 的
`session.snapshot.created` cursor 和 idle checkpoint 是 NZ-Coder 为 A021 reconnect protocol 增加的闭环，不是声称复制了 InfCode server。

### 24.2 NZ-Coder 原有不足

A021 虽然给 streaming text part 建了稳定 ID，但这些 ID 只存在于 EventBus：

- `save_session()` 仍只保存 role/content/tool_calls，service restart 后 `/messages` 无法与旧 part event 对账；
- 旧会话没有 message/part ID，若直接随机补 ID，每次 restart 都会变化；
- 410 后需要分别读取 messages/status/pending，再重新订阅，多个请求之间没有共同 event watermark；
- snapshot 若在 Agent 运行中直接读取，会遇到 `message.completed` 已发而 manager history 尚未 settle 的提交窗口；
- 如果 snapshot 没有真实 event ID，空 Session 或无近期事件时无法表达“从这个状态之后继续”。

### 24.3 实现结果

#### 向后兼容的持久 message schema

HTTP manager 的内部 history 现在为每条消息保存：

```text
_nz_message_id: msg-<opaque id>
_nz_session_id: <owning Session ID>
_nz_parts:
  - id: part-<opaque id>
    message_id: msg-<same owner>
    type: text
    text: ...
```

AgentLoop 完成 assistant text part 时，把与 `message.part.updated` 完全相同的最终 part snapshot 附到 assistant history；manager 为 user、
tool 以及自定义 Agent 返回但未带 identity 的消息补齐 metadata。HTTP settle 调用 `save_session()` 时写入
`message_schema_version=1` 和这些私有字段。
`_sanitize_messages()` 会剥离全部 `_nz_*` 字段，provider 不会收到协议 metadata。
auto-compaction 有独立的 summary provider 请求，因此 `_select_summary_input()` 也先投影 public message，再计算预算和序列化 transcript；
不能只保护主 completion 路径。

原有 `GET /session/{id}/messages` 与 `NZCoderClient.messages()` 继续返回既有 message dict，不暴露 `_nz_*`，因此 A018 客户端接口不破坏。
新增 `GET /session/{id}/snapshot` / `client.snapshot()` 返回：

```text
schema_version, snapshot_id
session: current idle Session info
messages[]:
  info: id + session_id + legacy message fields
  parts[]: complete persisted text-part snapshots
pending: {permissions: [], questions: []}
cursor: {event_id, sequence}
```

这只是 InfCode `WithParts` 的 text-first 子集，不是假装已经有 message/part 数据库。

#### 旧会话确定性迁移

载入没有 `_nz_message_id` 的历史文件时，A022 根据 session ID、message index 和规范化 message 内容生成 UUIDv5 identity；对应 text part
ID 再由 message ID 确定性派生。相同旧文件跨 service restart 得到相同 ID。非法持久 ID、Session owner 不匹配、非法 part
ID/type/text 不会直接出现在 snapshot，而是丢弃并从 legacy content 重建。合法的 persisted text part 可以和空 assistant content
并存，这是 tool-call preamble 的正常语义，并不表示本地 session JSON 具有防篡改认证。新 user/assistant message 仍使用随机 UUID，
避免泄露计数。

#### Idle snapshot checkpoint

```text
GET /session/{id}/snapshot
  -> acquire ManagedSession run lock
  -> reject with 409 if a run/thread is active
  -> SessionEventBus.checkpoint()
       -> acquire EventBus sequence/publish lock
       -> copy structured message + idle Session state
       -> publish session.snapshot.created(snapshot_id, message_count)
  -> return copied state + anchor event ID/sequence
  -> release locks
```

`checkpoint()` 在同一个 EventBus critical section 内完成 state factory 与 anchor publish；callback 只能复制状态，不能递归调用同一
EventBus。并发 publisher 只能排在 anchor 后。Manager run lock
又使新 run 无法在 state copy 与 anchor 之间启动。这样 snapshot cursor 本身一定存在，即使此前 Session 没有任何事件。

客户端完成 410 resync 的顺序变为：清除旧 cursor/provisional parts → 等待 Session settle（可先明确 abort，但仍须等到 settled）→
获取 `/snapshot` → 用
`snapshot.cursor.event_id` 发起 `Last-Event-ID` 连接。snapshot 后、订阅前产生的受支持 Session events 会由 strict-after replay 补回；
若 anchor 已离开 tail 而再次 410，则重新获取 snapshot，不回退到猜测 replay。

### 24.4 关键设计决策

#### Snapshot 只允许 idle，不伪造 running consistency

AgentLoop 的 run-local messages 会先产生 Part/completed event，manager 只在任务返回后提交 history。第一阶段若允许 running snapshot，必须再建
一个实时 message projector 或把 manager commit 与 EventBus 变成统一事务。A022 选择 409 的明确边界：可用性稍弱，但不会把时间窗口
包装成“原子”。这也保证 idle snapshot 中没有真实 pending interaction，所以 `pending` 明确为空。

#### 新 endpoint，不修改 `/messages`

大量 CLI、评测和既有 HTTP 测试依赖 role/content/tool_calls 列表。直接把它改成 `WithParts[]` 会违反“不破坏现有接口”。私有存储
metadata + additive `/snapshot` 让旧调用方完全不变，新 host 才选择结构化协议。

#### Anchor 必须是持久普通事件

返回 deque 当前最后一条 ID 在空 Session 时没有值，也无法证明 state copy 与 cursor 的先后。显式 `session.snapshot.created` 同时提供
真实 SSE ID、审计点和 journal 恢复机会。它不是业务消息，reducer 只把它当 resync barrier。

### 24.5 关键文件

- `nz_coder/message_schema.py`：identity 校验、旧会话确定性迁移、legacy projection 与 `WithParts` projection；
- `nz_coder/runtime/loop.py`：把最终 assistant text part 写入内部 message metadata，并在 provider 请求前剥离 metadata；
- `nz_coder/session_events.py`：state copy + anchor publish 的原子 `checkpoint()`；
- `nz_coder/http_service/manager.py`：持久 internal history、idle gate 与 snapshot response；
- `nz_coder/http_service/server.py`、`client.py`：`/snapshot` route 与薄客户端方法；
- `nz_coder/state/sessions.py`：持久 payload 的 `message_schema_version`；
- `tests/test_message_schema.py`、`test_session_events.py`、`test_http_service.py`：迁移、校验、checkpoint、HTTP resync 和 restart 覆盖。

### 24.6 验证结果

- Ruff：A022 源码和测试为 `All checks passed!`；
- `py_compile`：message schema、EventBus、Loop、HTTP manager/server/client 与 session persistence 通过；
- Message/Context/Event/HTTP 专项：`55 passed`；
- 聚焦回归：`155 passed`，覆盖 HTTP/Permission/Question/Plan/Message/Context/Event/CLI/Loop/Memory/Dodo；
- 完整回归：`633 passed`，观察到 3 个 websockets/uvicorn/multiprocessing fork 告警；
- `test_session_event_checkpoint_is_atomic_with_later_publish` 用并发 publisher 验证 state copy → anchor → later event 顺序；
- `test_agent_loop_publishes_native_session_lifecycle` 验证 event part 与持久 assistant part 使用同一 identity/snapshot；
- `test_http_idle_snapshot_has_persisted_message_parts_and_atomic_cursor` 验证 legacy API 不变、WithParts snapshot，以及 snapshot 后完成的
  整轮事件能从 anchor strict-after replay；
- `test_http_snapshot_rejects_running_session` 验证运行中明确 409；
- `test_http_restart_discovers_and_lazily_restores_session` 验证 schema metadata 落盘并在 dormant materialization 后保持 message ID；
- `test_duplicate_persisted_message_and_part_ids_are_normalized` 验证同一 Session 内 message/part collision 被确定性归一化；
- `test_auto_compact_does_not_send_message_protocol_metadata` 验证 auto-compaction 的独立 provider 请求同样剥离 `_nz_*`；
- `test_snapshot_checkpoint_blocks_a_concurrent_run_start` 验证 checkpoint 持锁期间新 run 不能越过 anchor；
- 三组 55/155/633 是逐级重叠集合，不能相加；按用户要求没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/message_schema.py nz_coder/session_events.py nz_coder/runtime/loop.py \
  nz_coder/http_service nz_coder/state/sessions.py \
  tests/test_message_schema.py tests/test_context_budget.py \
  tests/test_session_events.py tests/test_http_service.py
python3 -m py_compile \
  nz_coder/message_schema.py nz_coder/session_events.py nz_coder/runtime/loop.py \
  nz_coder/http_service/manager.py nz_coder/http_service/server.py \
  nz_coder/http_service/client.py nz_coder/state/sessions.py
python3 -m pytest -q \
  tests/test_message_schema.py tests/test_context_budget.py \
  tests/test_session_events.py tests/test_http_service.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_question.py \
  tests/test_plan_mode.py tests/test_message_schema.py tests/test_context_budget.py \
  tests/test_session_events.py \
  tests/test_cli_commands.py tests/test_loop_fake.py tests/test_memory.py \
  tests/test_dodo_integration.py
python3 -m pytest -q
```

### 24.7 学习重点

1. live event identity 若不进入持久 projection，restart 后的 UI reconciliation 仍然断裂。
2. 向后兼容升级适合用私有 storage metadata + additive endpoint，不必破坏旧消息接口。
3. 旧数据迁移 ID 必须确定性，否则每次启动都会让客户端以为全部 message 被删除并重建。
4. 原子 snapshot 不只是“一次 HTTP 返回多个字段”；必须定义 state copy 与 event watermark 的锁和顺序。
5. 没有既有 event 时也需要真实 anchor，不能用空 cursor 表达一致边界。
6. 无法保证 running state 一致时应返回 409，而不是给一个看起来完整、实际跨提交窗口的 snapshot。
7. snapshot cursor 仍受 retained tail 和 best-effort journal 限制；过期后应重新 snapshot。

### 24.8 设计边界与剩余差距

- 只持久 text part；reasoning、tool input/output、file、step、patch、question 等仍没有完整 `WithParts` schema；
- 内部 metadata 仍和 legacy messages 一起写 JSON，而不是 InfCode 的 message/part 表、SyncEvent projector 或可查询数据库；
- snapshot 只允许 idle；没有运行中 provisional part、pending interaction 或 active tool state projection；
- 历史无 ID message 使用 index/content 派生 identity；context compaction 改写或重排旧 message 时 identity 会相应变化；
- Session JSON 只做结构、owner 与 identity 归一化，没有 MAC/signature；拥有同等本地文件权限的进程可篡改合法形状的 content/part；
- A022 没有发布 `message.updated/removed` 或持久 part remove 事件；snapshot 是 resync 基线，不是完整 CRUD protocol；
- snapshot checkpoint 是内存锁边界，不是 conversation JSON 与 event journal 的跨文件磁盘事务。若 session persistence 失败，当前进程的
  snapshot 仍可用，但 restart 后可能回到旧 history；
- anchor journal append 仍是 best-effort，且 replay tail 有容量限制；snapshot 后订阅太晚收到 410 时必须重新 snapshot；
- subscriber queue 仍可能无通知 drop；atomic snapshot 修复的是 410 resync 空窗，不是 live slow-consumer gap（此项已由 A023 修复）；
- `/messages` 为兼容仍没有 ID；只有选择 `/snapshot` 的新 host 才获得 structured identity；
- 当前没有 SDK schema generation、ETag/conditional snapshot、分页、增量 message query 或 remote broker。

## 25. A023：HTTP 断流恢复与崩溃状态闭环

### 25.1 InfCode 参考能力

本项重新核对了当前本地 InfCode 的三条相关链路：

- `packages/opencode/src/server/routes/instance/event.ts`：instance SSE 使用 `AsyncQueue` 串接 Bus、`server.connected`、heartbeat、
  disconnect cleanup；当前 route 没有写 SSE ID，也没有 slow-consumer gap 帧；
- `packages/opencode/src/server/routes/instance/sync.ts`：SyncEvent 以 aggregate sequence 持久化，并提供 history/replay，说明成熟 host
  不能只依赖一次 live SSE 连接；
- `packages/opencode/src/session/status.ts`：busy/retry/idle 是 instance-local runtime status，状态变化会发布事件，但它不是 NZ-Coder
  进程崩溃后的持久恢复标记。

因此 A023 不是声称逐行复制 InfCode。它对齐的是“live stream 必须有可恢复基线、断流后必须能证明连续性”这一系统不变量；
NZ-Coder 继续沿用 A021/A022 已有的 SSE cursor + idle snapshot，并补齐本地有界队列和 JSON Session persistence 特有的缺口。

### 25.2 NZ-Coder 原有不足

A022 结束时，正常路径已经能从 snapshot anchor strict-after 续传，但仍有三处半闭环：

1. `SessionSubscription` 满时会删除队首再塞入新事件。客户端仍会看到更大的 event ID，却不知道中间漏过事件，之后用这个 ID
   重连会把缺口永久跳过；
2. 标准库客户端能有限重连，但 410 和 live queue gap 仍要调用方手工编排 snapshot，host 很容易漏写恢复分支；
3. 持久 Session 没有记录 accepted run 是否结算。服务进程在 run 中退出后，重启只显示 dormant/idle，看起来像任务正常完成。

这三项都属于正确性，而不是 GUI 或 App 功能。如果不收口，HTTP API 虽然 endpoint 齐全，状态同步仍不能称为完整闭环。

### 25.3 实现结果

核心恢复链路现在是：

```text
idle /snapshot
  -> state copy + persisted anchor cursor
  -> resilient_events(Last-Event-ID=anchor)
  -> ordered replay/live events
  -> session.run.settled

subscriber queue overflow
  -> 清空尚未交付的局部队列
  -> 订阅进入不可逆 gapped 状态，不再发送更新事件
  -> 无 SSE id 的 server.event_gap
  -> client 等待 Session idle
  -> 新 /snapshot + 新 anchor
  -> 从新基线继续

accepted run
  -> 原子持久 run_status=running（写入已接受 user message）
  -> Agent worker
  -> 原子持久 terminal run_status
  -> session.run.settled
```

新增或修改的行为：

- `SessionSubscription` 第一次 overflow 后不再“保留最新事件继续假装连续”，而是进入 gapped terminal state；
- `iter_sse()` 发出 `server.event_gap` 后关闭该 SSE。gap 帧故意没有 `id:`，不会推进客户端最后可信 cursor；
- `NZCoderClient.resilient_events()` 总是先产生 synthetic `server.snapshot` baseline；遇到 gap 或 HTTP 410
  `event_cursor_expired` 时，有限次等待 idle、重新 snapshot 并从新 cursor 连接；
- `save_session()` 增加向后兼容的可选 `run_status`，JSON 文件改为 temp + fsync + replace 原子替换；HTTP 明确按
  Session-ID 主文件判定 commit，`latest/active` alias 为 best-effort，CLI 默认仍保留 alias 写失败可见的旧语义；
- HTTP Session 创建时持久 `idle`，接受 run 前持久 `running`，提交历史时持久 terminal status；
- restart scan 发现 `run_status=running` 时公开 `interrupted`，保留已接受的 user message 和明确 `last_error`，允许用户再次 run；
- `SessionHTTPService.event_queue_size` 提供有界、可测试的 transport queue 配置，默认仍为 256。

### 25.4 关键设计决策

#### Gap 帧不能拥有 event ID

gap 是单个 transport subscription 的局部故障，不是 Session 的全局业务事件，所以不能写入 EventBus/journal。若 gap 帧带 ID，客户端可能把
这个局部通知当成新的可靠 cursor；A023 明确让它无 ID，并要求重新建立 snapshot baseline。

#### Overflow 后必须停止投递

仅报告“丢了 1 条”但继续发送后续事件仍然危险，因为 reducer 可能先应用 gap 后的 message/part 更新，再把其 ID 保存为 cursor。进入 gapped
状态后忽略后续 fan-out，使最后可信 cursor 保持在调用方已经完整消费的 frame 上。

#### 自动恢复放在 additive 高层方法

原有 `events()` 是低层 SSE iterator，调用方可能需要观察 raw 410 或自行保存 cursor。直接改变其返回序列会破坏接口。新增
`resilient_events()` 才产生 synthetic `server.snapshot`，老客户端行为不变，新 host 可以直接选择完整恢复协议。

#### Accepted run 状态与消息必须一次原子落盘

只写一个独立 running marker 会产生两文件提交顺序：marker 与 conversation 任一先写，崩溃都可能导致状态和历史不一致。A023 把
`run_status` 和已接受 message 放进同一个 Session JSON，并把整个 JSON 改为原子 replace。它仍不是 event journal 与 Session JSON 的
跨文件事务，但足以判断“这个 HTTP run 是否完成 manager commit”。

#### HTTP 到此冻结，不继续把 CLI 强行做成 App

本地 HTTP service 是可选的 headless/host boundary：CLI 可以继续直接使用 AgentLoop，未来 IDE、桌面壳或自动化调用方才通过 HTTP。
A023 完成后，不再为了“看起来像 App”继续扩 route、GUI 或完整 Part 类型。除非出现明确 consumer、协议正确性 bug 或用户重新指定目标，
HTTP 主线冻结，后续回到 NZ-Coder 与 InfCode 的核心 Agent 能力差距。

### 25.5 关键文件

- `nz_coder/session_events.py`：gapped subscription、结构化 gap exception 与无 ID SSE gap control；
- `nz_coder/http_service/client.py`：snapshot-first `resilient_events()`、410/gap 自动重同步和 idle wait；
- `nz_coder/http_service/manager.py`：create/accepted/settled 持久状态边界与 interrupted materialization；
- `nz_coder/http_service/server.py`：可配置的有界 subscriber queue；
- `nz_coder/state/sessions.py`：可选 `run_status` 与原子 JSON replace；
- `tests/test_session_events.py`、`tests/test_http_service.py`：overflow、gap frame、自动 rebase、崩溃恢复和真实 HTTP 闭环。

### 25.6 验证结果

- Ruff：本项源码与测试 `All checks passed!`；
- `py_compile`：EventBus、Session persistence、HTTP manager/server/client 通过；
- Event/HTTP 定向：`53 passed`；
- 聚焦回归：`207 passed`，覆盖 HTTP/Permission/Question/Plan/Message/Context/Event/CLI/Loop/Memory/Dodo/Smoke；
- 完整回归：`642 passed`，观察到 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- `test_session_event_sse_reports_queue_gap_without_advancing_cursor` 验证 overflow 不再静默且 gap 无 ID；
- `test_resilient_client_rebaselines_after_an_explicit_gap` 和 cursor expiration 测试验证两条自动 rebase 分支；
- `test_http_resilient_stream_closes_snapshot_to_settled_loop` 通过真实 loopback 服务验证 snapshot → strict-after SSE → settled →
  final snapshot；
- `test_http_restore_marks_an_unsettled_accepted_run_as_interrupted` 验证 running 文件重启后不会伪装 idle，并可显式 retry；
- filtered overflow + close 竞态测试验证 `session.disposed` 不会用 close sentinel 覆盖待交付 gap；
- acceptance/terminal persistence failure 测试验证拒绝前回滚、`persisted:false`、restart interrupted，以及 alias failure 不否定
  authoritative Session-ID commit；合法旧文件移除 `run_status` 后仍按 dormant/idle 恢复并可继续 run；
- 三组 53/207/642 是逐级重叠集合，不能相加；按用户要求没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/session_events.py nz_coder/state/sessions.py \
  nz_coder/http_service/manager.py nz_coder/http_service/server.py \
  nz_coder/http_service/client.py \
  tests/test_session_events.py tests/test_http_service.py
python3 -m py_compile \
  nz_coder/session_events.py nz_coder/state/sessions.py \
  nz_coder/http_service/manager.py nz_coder/http_service/server.py \
  nz_coder/http_service/client.py
python3 -m pytest -q tests/test_session_events.py tests/test_http_service.py
python3 -m pytest -q \
  tests/test_http_service.py tests/test_permissions.py tests/test_question.py \
  tests/test_plan_mode.py tests/test_message_schema.py tests/test_context_budget.py \
  tests/test_session_events.py tests/test_cli_commands.py tests/test_loop_fake.py \
  tests/test_memory.py tests/test_dodo_integration.py tests/test_smoke.py
python3 -m pytest -q
```

### 25.7 学习重点

1. bounded queue 不能把 drop 当普通性能指标；对增量状态协议而言，未通知的 drop 是一致性错误。
2. 最后可信 cursor 只能在完整 frame 被消费后推进；局部 gap 通知本身不应创造全局 cursor。
3. reconnect 只修复 TCP 断线，snapshot + cursor 才能修复“无法证明连续”的状态。
4. 自动恢复应有有限 retry 和 idle gate，不能在 running history 尚未提交时制造伪 snapshot。
5. “进程里没有 active worker”不等于上一次 run 已成功；持久 accepted/terminal 边界才能在 restart 后区分 interrupted。
6. 一个可选 HTTP host boundary 不等于必须把终端 Agent 产品化成桌面 App；协议闭环后及时冻结范围同样是架构工作。

### 25.8 剩余边界

- snapshot 仍只允许 idle；没有运行中 provisional part、pending interaction 或 active tool projector；
- `resilient_events()` 是 Python 标准库客户端能力，没有生成 TypeScript/Java/Kotlin SDK；
- Session JSON 与 event journal 仍是两个原子文件，不是单数据库事务；极窄崩溃窗口可能让 event tail 比 committed history 更新；
- slow subscriber resync 会丢弃该连接尚未交付的所有局部 queue 内容，以完整 snapshot 正确性换取增量连续；
- `interrupted` 表示 manager 未观察到 terminal persistence，不自动重放旧 run，避免重复外部副作用；
- 多进程同时写同一 Session 仍未协调；当前 service 的授权模型仍是一个 workspace 由一个本地 service owner 管理；
- 完整 reasoning/tool/file/step Part、远程鉴权、浏览器 host、GUI/IDE SDK 和配额不在这次闭环范围，也不作为默认下一步。

## 26. A024：MCP Session 生命周期与动态能力刷新

### 26.1 InfCode 参考能力

本项重新核对当前本地 InfCode：

- `packages/opencode/src/mcp/index.ts`：MCP Service 由 instance state 持有 client、status、tool definitions、prompt/resource cache；
- 同文件 `watch()`：分别监听 tools、prompts、resources 的 `list_changed`，后台刷新 cache，tools 变化后发布 `mcp.tools.changed`；
- 同文件 `ensureStarted()`、`connect()`、`disconnect()`：初始连接可后台发起，状态经历 connecting/connected/failed/disabled，
  运行时可重连而不重建整个 Agent；
- `packages/opencode/src/session/prompt.ts`：每次构造模型工具集合时重新读取 `mcp.tools()`，所以新工具能在后续模型轮次出现；
- `packages/opencode/src/server/routes/instance/mcp.ts`：提供 status/add/connect/disconnect/OAuth 管理边界。

InfCode 使用官方 TypeScript SDK，并同时支持 local stdio 和 remote Streamable HTTP/OAuth。A024 对齐的是 stdio runtime 生命周期、缓存和
动态刷新，不声称已经完成 remote transport 或 OAuth。

### 26.2 NZ-Coder 原有不足

A016 的工具发现和调用已经可用，但 lifecycle 仍是一次性包装：

```text
每次 AgentLoop.run
  -> 顺序启动 server A
  -> 顺序启动 server B
  -> 只读取 tools/list
  -> run finally 关闭全部 server
```

实际问题包括：

- 终端或 HTTP Session 的每次用户回合都重新拉起 npx/python server，重复初始化且丢失 server-side cache；
- 多个慢 server 的启动耗时直接相加；
- server 发出 `notifications/tools/list_changed` 后，模型仍使用旧 schema；
- prompts/resources 完全不可见；
- failed/disabled server 没有 connect/disconnect/reconnect 管理入口；
- 若简单把通知 handler 放在 stdout reader 中调用 `tools/list`，reader 会等待一个只能由自己读取的 response，形成确定性死锁；
- 生命周期提升后，evaluation/Dodo 等一次性 Agent owner 若不显式 close，会遗留 MCP 子进程。

### 26.3 实现结果

核心调用链变为：

```text
AgentLoop 首次 run
  -> 创建一次 MCPRuntime(workspace/session owned)
  -> ThreadPoolExecutor 并行 initialize + tools/prompts/resources list
  -> live dynamic-tool provider
  -> 后续 AgentLoop.run 复用相同 clients/cache
  -> AgentLoop.close 统一关闭 runtime

server list_changed notification
  -> stdout reader 只做解析与通知入队
  -> 独立 notification worker 调用对应 list API
  -> 校验 client identity 仍是当前 generation
  -> 原子替换 runtime cache/bindings
  -> 下一次 get_specs/dispatch 读取 live provider 快照
```

已增加：

- `MCPClient.list_prompts/get_prompt/list_resources/read_resource`，list API 都支持最多 100 页的 bounded cursor pagination；
- 单独的 notification worker、按 method 合并 pending 通知，以及 tools/prompts/resources 三类 handler；
- handler 安装前收到的通知按 method 有界暂存并在注册时 replay，关闭 initial discovery 的 list-changed 丢失窗口；
- `MCPRuntime.start()` 并行初始连接，`start_background()/wait_ready()` 提供非阻塞启动边界；
- startup 使用共享 readiness event 区分 new/starting/ready，多个并发调用等待同一启动 generation；
- `connecting/connected/failed/disabled` 状态，以及同步 `connect/disconnect/reconnect`；
- prompt/resource metadata cache 和显式 get/read；unsupported feature 不阻断 tool server 建连；
- JSON-RPC error 保留 numeric code：只有 `-32601 Method not found` 代表 optional capability 缺失，timeout/transport/protocol
  failure 会拒绝或退休该 client generation，并立即移除失效 bindings；
- `_pending_clients` generation guard，确保 disconnect/close 与后台 startup 竞态时旧 client 不能重新写回 connected；
- live ContextVar tool provider：不破坏 workspace/线程隔离，同时允许一次 run 的后续模型轮次看到更新后的工具；
- AgentLoop 将 runtime 生命周期从 per-run 提升到 Agent/Session，CLI、HTTP、evaluation 和 Dodo owner 补齐 close；
- CLI、evaluation 和 Dodo 的 owner 级 `finally` 同时覆盖 Agent run 与后处理异常，而不只覆盖正常返回；
- MCP cache/lifecycle 变化发布 secret-free `session.mcp.changed`。
- connected/disconnected/failed/cache-changed 与对应状态变更在同一 runtime 锁内线性化，避免断开后晚发旧 generation 事件。

### 26.4 关键设计决策

#### Notification handler 必须离开 stdout reader

JSON-RPC response 和 notification 共用 stdout。若 reader 收到 list-changed 后同步调用 `list_tools()`，该调用会等待 response，而 response
又必须由被占用的 reader 解析。A024 使用专属 worker；reader 只把已注册 method 放入有界队列，同 method 的重复 pending 通知合并。

#### Cache 是连接状态的一部分

每次生成 tool schema 都访问 server 会把网络/子进程延迟带入模型轮次，也会让某个坏 server 阻塞整个 Agent。A024 在 connect 和
list-changed 时更新 cache，普通 `tool_bindings()/prompt_definitions()/resource_definitions()` 只读内存。

#### Live provider 不写模块级 registry

直接更新 `TOOL_SPECS` 会让并发 workspace 看见彼此的 MCP handler。新增 ContextVar live provider，每次 get_specs/dispatch 从当前
Session 的 runtime 获取快照；子 Agent 的 disabled scope 同时清除静态 overlay 和 provider。

#### Agent/Session owner，而不是进程全局 singleton

复用必须有边界。进程全局 MCP manager 会重新引入 A014 已消除的 workspace 串线风险；per-run 又没有复用价值。因此 Runtime 由
AgentLoop 持有：CLI Session 和 HTTP ManagedSession 可跨回合复用，一次性 evaluation/Dodo owner 在任务后 close。

#### Optional prompt/resource failure 不应让 tools 失效

MCP server 可以只实现 tools。连接时 prompts/list 或 resources/list 明确返回 `-32601 Method not found`，会得到空 cache，但 tools
仍保持 connected。其他 timeout、transport 或 protocol failure 表示该 client generation 已不可信：启动阶段拒绝连接，动态刷新阶段则
原子移除 client/cache/bindings 并标为 failed。不能用 `except Exception: []` 把真实断线伪装成 feature absence。

### 26.5 关键文件

- `nz_coder/mcp/client.py`：prompt/resource 协议、notification queue/worker 与 handler；
- `nz_coder/mcp/runtime.py`：并行/后台 startup、状态、cache、list-changed、generation guard 和 lifecycle management；
- `nz_coder/tools/__init__.py`：ContextVar live dynamic-tool provider；
- `nz_coder/runtime/loop.py`：Agent-owned Runtime、change event 和 close；
- `nz_coder/evaluation/*.py`、`nz_coder/dodo/dev_headless.py`：一次性 owner 的资源收尾；
- `tests/fixtures/mcp_echo_server.py`：tools/prompts/resources 与三类 list-changed 的真实 stdio fixture；
- `tests/test_mcp.py`：协议、缓存刷新、并行启动、管理 API、竞态和跨 run 复用测试。

### 26.6 验证结果

- Ruff：本项源码和测试 `All checks passed!`；
- `py_compile`：tools、MCP client/runtime、AgentLoop、evaluation 和 Dodo owner 通过；
- MCP 定向：`33 passed`；
- 聚焦回归：`161 passed`，覆盖 MCP/Loop/Dodo/evaluation/parallel/permissions/Event/HTTP/CLI；
- 完整回归：`656 passed`，观察到 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- 真实 subprocess fixture 验证 initialize、tools/prompts/resources、调用、三类通知和进程清理；
- barrier 测试证明两个 server 的 start 实际并发，而不是只把顺序启动包进一个后台线程；
- disconnect/startup 竞态测试证明 disabled 状态胜过晚到的 startup completion；
- concurrent start 测试证明调用方等待同一个 readiness generation，connect/disconnect watcher 竞态不会发布晚到 connected；
- pre-handler notification 测试证明 initial discovery 期间的 list-changed 会在 handler 注册后 replay；
- evaluation、Dodo 后处理和 CLI run 的异常注入测试证明 owner 仍会 close Agent；
- JSON-RPC code、optional discovery transport failure 和 list refresh failure 测试证明只有明确 Method not found 才降级，断线不会保留 stale tool；
- lifecycle handler barrier 测试证明 connected 与 disconnected 事件遵循状态线性化顺序；
- live provider 测试证明同一 ContextVar scope 内，通知前看不到 `mcp_echo_fresh`，刷新后 get_specs 可见；
- 按用户要求没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/tools/__init__.py nz_coder/mcp nz_coder/runtime/loop.py \
  nz_coder/evaluation/benchmark.py nz_coder/evaluation/eval_runner.py \
  nz_coder/evaluation/aider_benchmark.py nz_coder/dodo/dev_headless.py \
  tests/test_mcp.py tests/fixtures/mcp_echo_server.py
python3 -m py_compile \
  nz_coder/tools/__init__.py nz_coder/mcp/client.py nz_coder/mcp/runtime.py \
  nz_coder/runtime/loop.py nz_coder/evaluation/benchmark.py \
  nz_coder/evaluation/eval_runner.py nz_coder/evaluation/aider_benchmark.py \
  nz_coder/dodo/dev_headless.py
python3 -m pytest -q tests/test_mcp.py
python3 -m pytest -q \
  tests/test_mcp.py tests/test_loop_fake.py tests/test_dodo_integration.py \
  tests/test_eval_runner.py tests/test_parallel_benchmark.py tests/test_permissions.py \
  tests/test_session_events.py tests/test_http_service.py tests/test_cli_commands.py \
  tests/test_dodo_dev_headless.py
python3 -m pytest -q
```

### 26.7 学习重点

1. “进程启动在后台”不等于多 server 并行；需要用 barrier 等并发证据验证真正同时进入 startup。
2. 双向协议的 reader 线程不能执行会等待同一 reader 的请求，通知必须转交独立执行单元。
3. list-changed 是 edge trigger；刷新后仍必须检查 client generation，避免旧连接覆盖 reconnect 后的新 cache。
4. 长生命周期资源必须跟随有明确 close 的 owner；从 per-run 改为 per-session 时要审计所有非交互入口。
5. 动态 schema 和 handler 同样需要 ContextVar 隔离，不能因为“会变化”就退回模块级全局注册。
6. prompts/resources 是可选 capability；feature absence 与 transport failure 的处理强度应不同。
7. “正常退出会 close”不是生命周期闭环；资源 owner 必须用 `finally` 覆盖主任务和全部后处理异常。
8. optional capability 只能根据明确协议错误码降级；广泛吞异常会把 transport failure 伪装成可用连接。

### 26.8 剩余差距

- 仍只支持 local stdio；没有 Streamable HTTP、legacy SSE、headers、OAuth、token storage 或浏览器 callback；
- 没有读取 InfCode 的 `.infcode/mcpServers/mcp.json`、marketplace 安装或文件 watcher/reconcile；配置仍来自显式
  `NZ_MCP_SERVERS_JSON`；
- prompt/resource 已有 runtime API 和 cache，但尚未接入 CLI slash command、HTTP route 或模型可调用的专门工具；
- 不支持 resources/templates、subscribe、sampling、roots、logging、progress 和 cancellation notification；
- `start_background()` 是可选 API；AgentLoop 第一次 run 仍等待并行初始发现完成，保证首轮 schema 确定性；
- 手写 JSON-lines client 仍没有官方 MCP SDK 的完整 schema/version negotiation 和互操作矩阵；
- 子 Agent 仍不继承父 Session MCP；若未来开放，应为 child 显式创建权限和生命周期隔离的 Runtime；
- 未连接第三方公开 MCP server，本轮真实互操作仍使用仓库内 fixture；
- `mcp_` 工具名前缀继续保留，与 InfCode `<server>_<tool>` 不完全相同，这是权限来源可识别性的既有设计。

## 27. A025：Provider 精确模型目录与 reasoning variants

### 27.1 InfCode 参考能力

本项重新阅读当前本地 InfCode：

- `packages/opencode/src/provider/provider.ts` 的 `Provider.Model` 同时保存 provider/model identity、capabilities、context/input/output
  limit、status、options、headers、release date 和 variants；
- 同文件会合并 models.dev、用户 provider 配置和 model-specific variants，而不是仅根据 model id 子串猜测能力；
- `packages/opencode/src/provider/transform.ts` 的 `variants()` 根据 provider SDK 和精确 model family 生成 reasoning effort、thinking
  budget、thinking level 等选项；
- 同文件的 `providerOptions()` 把 variant/options 路由到不同 SDK namespace，`maxOutputTokens()` 让上下文预算与请求上限共享同一模型记录；
- `packages/opencode/src/session/system.ts` 根据精确模型选择 Anthropic、Codex、Gemini、GPT 等 prompt family；
- `packages/opencode/src/session/prompt.ts` 把用户选择的 variant 持久在 model selection 中，后续请求使用同一选择。

InfCode 有在线 models.dev 数据、更多 SDK 和 Responses API。A025 对齐的是精确记录优先、variant 选择、请求映射与 Session
snapshot，不声称复制完整 provider marketplace。

### 27.2 NZ-Coder 原有不足

A015 已建立不可变 `ModelCapabilities`、family rules、预算和三种 adapter，但仍有四个结构性问题：

```text
MODEL_PROVIDER + MODEL_ID
  -> model id substring rule
  -> one capability record
  -> no exact provider/model override table
  -> no selectable variant
```

- 私有部署或同名模型只能用一个全局 `MODEL_CAPABILITIES_JSON`，不能同时描述多个 provider/model；
- reasoning 只有 `supports_reasoning: bool`，无法表达 low/high、instant/thinking、thinking budget 等可选模式；
- Anthropic、Gemini 和 OpenAI-compatible 请求没有共享的 variant identity，配置容易只影响 prompt/budget 而没有真正进入 wire payload；
- `MODEL_PROVIDER=deepseek/kimi/openrouter/dashscope` 会被拒绝，即使它们使用已经支持的 OpenAI-compatible transport；
- 若每次请求重新读可变目录，Agent 构造时使用的预算/prompt 与后续请求参数可能在同一 Session 中漂移。

### 27.3 实现结果

新的解析优先级为：

```text
provider + model id
  -> conservative builtin family rule
  -> exact local catalog record: <provider>/<model>
  -> explicit context/output limits
  -> MODEL_CAPABILITIES_JSON final override
  -> active-model MODEL_VARIANT validation/selection
  -> immutable Agent capability snapshot
  -> provider-specific request mapping
```

已增加：

- `MODEL_CATALOG_JSON`：内联多模型目录；
- `MODEL_CATALOG_PATH`：从当前 workspace 内加载最大 2 MB 的 JSON 目录，resolved path 不能逃逸；使用 nonblocking/no-follow fd、
  `fstat` 和 bounded read 拒绝 FIFO、symlink race 与并发增长超限，再按 path/mtime/size 缓存并返回副本；
- exact key `<normalized-provider>/<exact-model-id>`，精确记录可覆盖 family、prompt family、context/output、tool/stream/reasoning/
  temperature 等现有 capability 字段；
- `MODEL_VARIANT`：只应用于当前 `MODEL_ID`，备用/子 Agent 的不同模型不会错误继承一个不兼容 variant；
- 不可变 `available_variants`、`selected_variant` 和 canonical JSON options snapshot；
- 内置 qwen `instant/thinking` 与 GPT reasoning `low/medium/high` variant；Anthropic/Gemini 可通过 exact catalog 声明其 native options；
- OpenAI-compatible 映射 `reasoning_effort/top_p/extra_body`，Anthropic 映射 `thinking/output_config.effort`，Gemini 映射
  `generationConfig.thinkingConfig`；
- variant option 使用 provider-specific allowlist 和逐字段类型校验；`extra_body` 当前只允许布尔 `enable_thinking`，不能嵌套覆盖
  model、messages、tools、headers 或凭据；
- exact catalog 中省略 `variants` 才允许 builtin 推导；显式 `{}` 保持禁用，且 builtin variants 在最终 capability override 后生成；
- 内建 adapter 接受 Agent-owned `_capabilities` 内部 snapshot 并在发请求前移除该字段，第三方 provider fake 和 wire payload 不会看到内部对象；
- 核心 prompt builder 不再独立解析模型；Agent 用自己的 snapshot 追加 family guidance；自动压缩、模型 `compact` 工具和 CLI `/compact`
  都经 Agent 的 `_compact_messages()` 使用同一 provider/model/snapshot；
- `openai/dashscope/deepseek/kimi/moonshot/openrouter/groq/mistral/together/cerebras/xai/zhipu/siliconflow` 等命名实例复用同一
  OpenAI-compatible adapter，但保留真实 provider identity 供精确目录匹配；
- `run_start` trace 记录 selected/available variants 和 capability source。

目录示例：

```json
{
  "models": {
    "deepseek/private-reasoner": {
      "family": "private-coder",
      "prompt_family": "codex",
      "context_tokens": 196000,
      "output_tokens": 48000,
      "supports_reasoning": true,
      "variants": {
        "low": {"reasoning_effort": "low"},
        "high": {"reasoning_effort": "high"}
      }
    }
  }
}
```

### 27.4 关键设计决策

#### 精确记录覆盖 family rule，而不是删除 fallback

完全依赖在线模型库会引入网络、可用性和供应链依赖，也不适合离线评测。family rule 继续为零配置模型提供保守默认；本地 exact
record 在需要准确性时覆盖它。目录数据由用户显式提供，不会自动上传模型名或下载远程数据。

#### Variant options 必须按 adapter 限制

若允许目录把任意 JSON 合并进请求，它可以静默替换 model/messages/tools，甚至形成不受审计的 header/credential 通道。A025 只允许三类
adapter 已实现并测试的字段；未知 option 在启动解析时直接报错。

#### Agent 持有 capability snapshot

模型窗口、prompt family、compaction 和请求 variant 必须来自同一记录。核心 prompt builder 不预先猜 family；Agent 初始化时解析一次并追加
guidance，内建 provider 的普通、planning、replanning、streaming、non-streaming、compaction 和 child 请求都接收对应 snapshot，避免目录文件
修改后出现“旧 prompt/预算 + 新请求参数”。内部 `_capabilities` 在 adapter 内消费，不进入 HTTP JSON 或 OpenAI SDK 调用。

#### 一个 transport adapter 可以有多个 provider identity

DeepSeek、Kimi、OpenRouter 等无需复制同构客户端。命名实例保留 provider identity，用于 exact catalog key 和 trace；认证与 endpoint 仍由
现有 `API_KEY/API_BASE_URL` 显式配置。这是协议复用，不表示所有服务具有相同模型能力。

#### 全局 variant 不传播给不同模型

`MODEL_VARIANT` 描述当前 `MODEL_ID` 的选择。子 Agent 若使用 `SUBAGENT_EXPLORE_MODEL`，会重新解析自己的模型但不继承该字符串，避免主模型的
`thinking` 被错误应用到只提供 `low/high` 的子模型。

### 27.5 关键文件

- `nz_coder/providers/capabilities.py`：exact catalog、bounded loader、variant validation/selection、request options 和 capability snapshot 数据；
- `nz_coder/providers/openai_compatible.py`：命名 provider identity、snapshot 消费和 OpenAI-compatible variant 映射；
- `nz_coder/providers/anthropic.py`：native thinking/effort payload；
- `nz_coder/providers/gemini.py`：native thinkingConfig payload；
- `nz_coder/providers/__init__.py`：兼容 provider 名称和公开 registry API；
- `nz_coder/runtime/loop.py`：Session snapshot 传递与 trace；
- `nz_coder/runtime/prompt.py`、`nz_coder/state/context.py`：Agent-owned family guidance 与 provider-aware compaction；
- `nz_coder/runtime/hooks.py`、`nz_coder/interface/commands/handlers/core.py`：手动工具与 CLI 压缩复用 Agent-bound compact 路径；
- `nz_coder/runtime/subagent.py`：子模型独立 snapshot；
- `nz_coder/config.py`、`.env.example`：catalog/variant 配置；
- `tests/test_model_capabilities.py`、`tests/test_native_providers.py`、`tests/test_providers.py`：精确记录、路径、variant 和 adapter contract。

### 27.6 验证结果

- Ruff：本项 Provider、Runtime 和测试 `All checks passed!`；
- `py_compile`：Provider modules、AgentLoop 和 Subagent 通过；
- Provider 定向：`56 passed`；
- Provider/Context/Subagent/Loop/Hook/CLI 聚焦回归：`140 passed`；
- 完整回归：`683 passed`，观察到 3 个既有 websockets/uvicorn/multiprocessing fork 告警；
- exact catalog 测试覆盖精确 key 优先级、内联/文件目录、workspace 逃逸、非法字段、unknown variant 和命名 provider identity；
- catalog 安全测试覆盖 FIFO/non-regular file、bounded read、嵌套 request override、option 类型、显式空 variants 和 final override；
- adapter contract 测试证明 OpenAI-compatible、Anthropic、Gemini 三种 variant 进入正确 payload；
- snapshot 测试证明内部 capability 不会传给 OpenAI SDK；
- prompt/compaction 测试证明 family guidance、GPT-5 token 参数和 reasoning variant 均来自 Agent snapshot，且自动、工具和 CLI
  三条 compaction 入口不再绕过 adapter；
- 按用户要求没有运行 SWE-bench 官方评测，也没有使用真实 API 凭据做 live provider smoke。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/config.py nz_coder/providers nz_coder/runtime/prompt.py \
  nz_coder/runtime/loop.py nz_coder/runtime/subagent.py nz_coder/runtime/hooks.py \
  nz_coder/state/context.py nz_coder/interface/commands/handlers/core.py \
  tests/test_model_capabilities.py tests/test_context_budget.py \
  tests/test_native_providers.py tests/test_providers.py tests/test_hooks.py \
  tests/test_cli_commands.py
python3 -m py_compile \
  nz_coder/providers/*.py nz_coder/runtime/prompt.py nz_coder/runtime/loop.py \
  nz_coder/runtime/subagent.py nz_coder/state/context.py
python3 -m pytest -q \
  tests/test_model_capabilities.py tests/test_native_providers.py \
  tests/test_providers.py tests/test_provider_smoke.py
python3 -m pytest -q \
  tests/test_model_capabilities.py tests/test_native_providers.py \
  tests/test_providers.py tests/test_provider_smoke.py tests/test_context_budget.py \
  tests/test_subagent.py tests/test_loop_fake.py tests/test_hooks.py \
  tests/test_cli_commands.py
python3 -m pytest -q
```

### 27.7 学习重点

1. `supports_reasoning=True` 只说明能力存在，不能表示用户选择了哪种 reasoning policy。
2. 模型 identity 必须包含 provider；同一个 model id 在不同 gateway 下可能有不同窗口、参数和工具能力。
3. 精确目录和 family fallback 不是二选一：fallback 解决零配置，exact record 解决可验证准确性。
4. prompt、context budget 和 wire options 必须绑定到同一不可变 snapshot，否则运行中配置变化会产生内部矛盾。
5. Provider 扩展首先应复用协议 adapter；只有 wire protocol/response semantics 不同时才新增 native adapter。
6. 配置驱动的 request options 仍是输入边界，必须限制字段、JSON 类型、文件大小和路径。
7. 文件读取的 `stat().st_size` 不是安全上限；需要对已打开 fd 验证 regular file，并对真实读取字节数设硬界限。

### 27.8 剩余差距

- 没有 models.dev 在线目录、价格、release date、status、input/output modality、attachment 或 model availability 数据；
- 没有 InfCode 的 provider/model list API、默认模型发现、CLI/HTTP model picker 或 Session 内动态切换；
- 目录是显式 JSON，不支持 YAML、远程 URL、签名 catalog、文件 watcher 或自动 reconcile；已存在 Agent 不热切换 snapshot；
- exact record 数值由用户维护，NZ-Coder 不向 live provider 验证 context/output 或 variant 可用性；
- 原生 adapter 仍只有 Anthropic Messages 和 Gemini generateContent；OpenAI-compatible 仍基于 Chat Completions，没有 Codex/OpenAI Responses API；
- 命名 OpenAI-compatible provider 共享 `API_KEY/API_BASE_URL` 配置，没有每-provider credential/env resolution；
- Variant allowlist 只覆盖当前三类 adapter 已实现字段，没有 headers、prompt caching、reasoning summary/encrypted content、topK、sampling 等完整选项；
- Anthropic/Gemini variant 需要 exact catalog 显式声明，未维护一个会迅速过时的内置精确模型清单；
- 未使用真实 API 凭据做 text/tool/stream/variant live smoke，只有离线 transport contract；
- 不自动安装 Provider SDK，也没有 InfCode 的二十余种 adapter 生态。

## 28. A026：MCP Streamable HTTP 远程传输

### 28.1 InfCode 参考能力

本项重新阅读了当前本地 InfCode 源码：

- `infcode-dev/infcode-dev/packages/opencode/src/config/mcp.ts`：local/remote 配置联合类型，remote URL、headers、OAuth 和 timeout；
- `infcode-dev/infcode-dev/packages/opencode/src/mcp/index.ts`：优先 Streamable HTTP、失败后旧版 SSE fallback、连接超时、OAuth 状态和 client 生命周期；
- `infcode-dev/infcode-dev/packages/opencode/src/mcp/auth.ts`：按 server URL 绑定的 token/client/state 存储和 `0600` 权限；
- `infcode-dev/infcode-dev/packages/opencode/src/mcp/oauth-provider.ts`、`oauth-callback.ts`：动态 client registration、PKCE、浏览器回调和 CSRF state 校验。

InfCode 的“远程 MCP”不是一个 HTTP POST wrapper，而是三层能力：Streamable HTTP transport、旧 SSE transport fallback、OAuth
授权生命周期。本轮只把第一层在 NZ-Coder 内做成完整闭环，并提供不落盘的显式 header credential；后两层仍明确保留为差距。

### 28.2 NZ-Coder 原有不足

A024 后的 MCP runtime 已经拥有 Agent/Session 生命周期、工具/提示词/资源缓存和 list-change 刷新，但只能执行本地 stdio command：

```text
NZ_MCP_SERVERS_JSON
  -> command + cwd + env
  -> MCPClient
  -> subprocess stdin/stdout newline JSON-RPC
```

因此远程服务存在以下实际断点：

- 配置 schema 不接受 remote URL 和 request headers；
- runtime 只能创建 subprocess client；
- 没有 Streamable HTTP 的 `Mcp-Session-Id` 建立、复用和关闭；
- 不解析 POST 返回的 `text/event-stream`，也没有 GET server-event stream；
- 若直接使用 `urllib` 默认 opener，环境中的 `HTTP_PROXY` 可能静默转发 bearer credential 和 session；
- 若允许 redirect，凭据可能被带到配置 URL 之外；
- inline `Authorization` 容易把 secret 写进项目配置或 trace。

### 28.3 实现结果

新的远程调用链为：

```text
NZ_MCP_SERVERS_JSON remote record
  -> config 校验 HTTPS / header_env / timeout / effect
  -> MCPRuntime 按 transport 选择 MCPHTTPClient
  -> POST initialize
  -> 捕获 Mcp-Session-Id
  -> POST notifications/initialized
  -> GET text/event-stream（server 不支持 404/405 时退化为 POST-only）
  -> tools/prompts/resources discovery 与既有 cache/binding
  -> POST tools/call / prompts/get / resources/read
  -> close: 停止事件流 + best-effort DELETE session
```

已经闭环的行为：

- remote config 支持显式 `type: "remote"`，也可通过存在 `url` 安全推导；local 原配置保持兼容；
- 默认只允许 HTTPS；开发测试只有 `localhost/127.0.0.1/::1` 加 `allow_insecure_http=true` 才允许 HTTP；
- URL 拒绝 userinfo credential、fragment、非 HTTP(S) scheme 和无 host 值；
- `headers` 承载普通固定值；已知 credential header（Authorization、Proxy-Authorization、Cookie、API-Key、X-API-Key）必须通过
  `header_env` 指向环境变量，缺失或含控制字符时连接失败且错误不泄露值；其他自定义敏感 header 也应主动使用 `header_env`；
- 禁止用户覆盖 Host、Content-Length、Content-Type、Accept、Connection、协议版本和 Session-ID 等 transport-owned header；
- 完全禁用 ambient proxy，并拒绝 3xx redirect，credential/session 只发往用户配置的固定 endpoint；
- HTTP client 请求 `2025-06-18`，只接受 `2025-03-26`/`2025-06-18` 两个 Streamable HTTP 协议版本，并把协商结果用于后续 header；
- 支持 JSON 与 SSE 两种 POST response；SSE 增量读取并在匹配 JSON-RPC id 到达时返回，不等待保持开放的 stream EOF；JSON-RPC
  batch body 也按多 message 解码；
- initialize response 捕获 1–1024 字节 visible-ASCII Session-ID，后续 POST、GET 和 DELETE 复用；非法或中途更换的 Session-ID 会失败；
- GET SSE 的单条或 JSON-RPC batch 通知接入 A024 的有界/coalesced notification queue；handler 注册前到达的 list-change 仍可 replay；
- GET 返回 404/405 被视为规范允许的 POST-only 服务，不让可选通知通道破坏工具调用；
- body/单个 SSE event 上限 10 MiB、SSE 单行上限 1 MiB，socket inactivity timeout 映射为 `MCPTimeoutError`；
- GET 的非 404/405 HTTP 错误、非法 content type 和读取失败进入 transport error，并通过 callback 让 runtime 原子退休 stale binding；
- runtime 继续复用已有工具 effect、权限、untrusted output、prompt/resource cache、connect/disconnect/reconnect 和 owner close 语义。

远程配置示例：

```bash
export NZ_MCP_ENABLED=1
export NZ_REMOTE_MCP_AUTH='Bearer replace-me'
export NZ_MCP_SERVERS_JSON='{
  "servers": {
    "remote": {
      "type": "remote",
      "url": "https://mcp.example.com/mcp",
      "header_env": {"Authorization": "NZ_REMOTE_MCP_AUTH"},
      "tool_effects": {"search": "read", "update": "write"}
    }
  }
}'
```

### 28.4 关键设计决策

#### Streamable HTTP 复用 A024 runtime，而不是建立第二套工具系统

stdio 和 HTTP 只应在 transport 层不同。两者初始化后都提供相同的 tools/prompts/resources API，因此动态工具命名、权限、缓存、通知刷新和
Session owner 继续由 `MCPRuntime` 管理。这样远程能力不会绕开 A024 已验证的隔离边界。

#### 不使用环境代理，也不跟随 redirect

MCP header 可能包含长期 bearer token。当前进程的 `HTTP_PROXY` 可能由开发环境、容器或 shell profile 注入，默认 `urllib` opener 会使用它；
redirect 又可能把请求转向另一个 origin。本实现使用显式空 `ProxyHandler` 和 no-redirect handler，使凭据的网络目标与配置文本一致。

#### 常见 credential header 只允许环境变量引用

InfCode schema 允许任意 inline headers，适合成熟的配置/secret UX；NZ-Coder 当前没有加密 secret store。允许常见 credential header inline 会
鼓励把 token 提交进项目 JSON。因此本阶段采用更窄的 `header_env`：配置保存变量名，值只在创建 client 时解析并再次校验控制字符，
status/trace 不包含 headers。无法穷举所有私有 header 名，未知的自定义 secret header 仍由配置作者选择 `header_env`。

#### OAuth 不混入本阶段

真正 OAuth 不是“再加一个 Authorization 字段”。它至少需要 protected-resource/auth-server metadata discovery、PKCE、随机 state、loopback callback、
dynamic client registration 或预注册 client、token refresh、按 server URL 防混淆的 `0600` storage，以及取消/超时/并发 flow 状态机。只做其中一半会
制造比“不支持”更危险的安全假象，所以 A026 明确以现有 bearer/header credential 完成 transport 闭环，OAuth 单列下一阶段。

#### 不自动读取项目内 MCP command 配置

当前入口仍是显式 `NZ_MCP_SERVERS_JSON`。若自动读取仓库中的 MCP config，本地 server record 会变成“打开项目即执行仓库指定命令”的供应链入口。
project config reconcile 必须先设计 trust/approval 和 local/remote 差异化策略，不能为追求表面对齐而静默启用。

### 28.5 关键文件

- `nz_coder/mcp/http_client.py`：Streamable HTTP、JSON/SSE、GET 通知流、Session-ID、timeout/size/redirect/proxy 和 DELETE；
- `nz_coder/mcp/config.py`：local/remote 联合配置、URL/header/env 安全校验；
- `nz_coder/mcp/runtime.py`：按 transport 创建 stdio 或 HTTP client，并复用现有 lifecycle/cache/binding；
- `nz_coder/mcp/__init__.py`：公开远程 client；
- `.env.example`：remote header credential 配置示例；
- `tests/test_mcp_http.py`：真实 loopback Streamable HTTP fixture 和端到端 contract。

### 28.6 验证结果

- Ruff：MCP modules 和两组 MCP tests `All checks passed!`；
- `py_compile`：`nz_coder/mcp/*.py` 全部通过；
- MCP 聚焦回归：`61 passed`，其中新增 remote 测试收集为 28 项；
- 真实 loopback contract 覆盖 Streamable HTTP 版本协商、initialize、JSON/SSE POST、保持开放的 POST SSE、Session-ID 后续复用、GET early
  notification replay、GET failure runtime retirement、工具/提示词/资源、runtime binding 和 DELETE；
- 安全测试覆盖非 loopback HTTP、URL credential/fragment/非法端口、inline 常见 credential、header/env 控制字符、transport header override、
  ambient proxy 和 redirect 拒绝；
- 排除 `tests/test_dodo_hosted_api.py` 后完整回归：`707 passed, 3 warnings`；
- 未排除的 `pytest -q` 在 collection 阶段失败：该既有 Dodo test 导入当前 pytest 环境无法解析的
  `tests.test_dodo_ingress`，尚未执行任何测试；本轮没有修改 Dodo 测试来掩盖这个独立问题；
- 没有使用公网第三方 MCP server 或真实 bearer credential；没有运行 SWE-bench 官方评测。

复现命令：

```bash
python3 -m ruff check nz_coder/mcp tests/test_mcp.py tests/test_mcp_http.py
python3 -m py_compile nz_coder/mcp/*.py
python3 -m pytest -q tests/test_mcp_http.py tests/test_mcp.py
python3 -m pytest -q --ignore=tests/test_dodo_hosted_api.py
```

### 28.7 学习重点

1. MCP remote transport 不是普通 REST：initialize 建立的 Session-ID 必须贯穿 POST、GET 和 DELETE。
2. Streamable HTTP 的 response 既可能是 JSON，也可能是 SSE；server notification 又可能来自独立 GET stream。
3. POST SSE 不能以 EOF 作为 request 完成条件；长连接中匹配 id 的 response 到达时就应停止等待并关闭该 response stream。
4. GET stream 是可选的，404/405 应稳定降级；认证/5xx/协议错误不能伪装成正常降级，必须让 runtime 退休旧 binding。
5. credential 安全不仅是“不打印 token”；ambient proxy、redirect、URL userinfo、控制字符和用户覆盖 transport header 都是外传路径。
6. 远程传输和工具运行时是两个边界：transport 可以替换，权限/effect/cache/owner 不能复制出分叉实现。
7. OAuth 是独立安全子系统，缺少 state、PKCE、URL binding 或安全存储时不应声称支持。

### 28.8 剩余差距

- 没有 InfCode 的 OAuth auto-discovery、PKCE、dynamic client registration、浏览器 callback、token refresh 和 `0600` token store；
- 没有旧版 `SSEClientTransport` fallback，只支持现代 Streamable HTTP；
- GET SSE 当前建立一次，不实现 Last-Event-ID、断流自动重连、server retry hint 或 resumability；
- request timeout 是 socket inactivity timeout，不是抵抗持续 slow-drip response 的总墙钟 deadline；活动 POST 也没有由 `close()` 主动中断的
  response registry；
- 未实现 server-to-client JSON-RPC requests，只处理 response 和 notification；
- 不支持 HTTP compression、custom CA/mTLS、per-server proxy 或 DNS/IP allowlist；
- 没有 project/user config merge、文件 watcher、trust approval 或运行中 config reconcile；
- 没有第三方公网 MCP server interoperability smoke，本轮证据是标准库真实 loopback contract；
- remote status 仍只显示异常类型，不区分 `needs_auth`、`needs_client_registration` 等 InfCode OAuth 状态；
- 已知 credential header 来自进程环境变量，但无法识别任意私有 header 名；没有 OS keychain 或独立 per-provider secret manager。

## 29. A027：MCP OAuth 授权生命周期

### 29.1 InfCode 参考能力

本项参考当前本地 InfCode：

- `packages/opencode/src/config/mcp.ts`：remote MCP 的 OAuth config，以及 `oauth: false` 显式禁用；
- `packages/opencode/src/mcp/oauth-provider.ts`：redirect URI、client metadata、URL-bound client/token、PKCE verifier 和 state；
- `packages/opencode/src/mcp/oauth-callback.ts`：loopback callback、state 校验、timeout、cancel 和成功/失败页面；
- `packages/opencode/src/mcp/auth.ts`：token、dynamic client、verifier/state 的 `0600` JSON store；
- `packages/opencode/src/mcp/index.ts`：`needs_auth`、start/finish/authenticate/remove/status 和认证后重新连接 MCP client。

InfCode 借助官方 MCP SDK 完成 OAuth discovery/transport challenge 和 token 操作。NZ-Coder 不能引入该 SDK，因此本项用标准库手写相同的核心
安全不变量，并把授权入口接到现有 CLI 和 A026 runtime。

### 29.2 NZ-Coder 原有不足

A026 已能安全传输静态 `Authorization` header，但没有授权生命周期：

```text
remote MCP 返回 401
  -> HTTP client 只知道 requires authentication
  -> runtime 无 token 可重试
  -> 用户只能手动创建长期 bearer env
```

具体缺口包括：

- 不知道 authorization server、authorization endpoint 和 token endpoint；
- 没有 PKCE verifier/challenge、随机 state 和 CSRF callback 校验；
- 不支持预注册 client secret 或 dynamic client registration；
- access token 过期后不能 refresh；
- 没有绑定 MCP server URL 的凭据库，改 URL 后可能误用旧 token；
- runtime 只显示一般 `failed`，用户不知道应执行授权；
- 没有 auth/status/logout 的用户入口。

### 29.3 实现结果

授权调用链：

```text
nz-coder mcp auth <server>
  -> 读取同一 MCPServerConfig
  -> protected-resource metadata discovery
  -> authorization-server metadata discovery
  -> configured client 或 dynamic registration
  -> 生成 256-bit state + PKCE S256 verifier/challenge
  -> 先绑定 127.0.0.1 callback，再打印/打开 authorization URL
  -> callback exact path + constant-time state validation
  -> authorization_code + verifier 换 token
  -> URL-bound atomic 0600 credential store
  -> 下次 MCPRuntime 创建 HTTP client 时注入 Bearer token
```

已实现：

- remote config 的 `oauth` 可为 object 或 `false`；省略时允许自动 OAuth，静态 bearer 配置可显式设 `false`；
- OAuth config 支持 `client_id`、`client_secret_env`、`scope`、`redirect_uri` 和显式 `authorization_server`；
- client secret 不能 inline，只能引用环境变量；callback 固定为带显式端口/路径的 `http://127.0.0.1/...`；
- 按 resource path 和 origin 两个 well-known candidate 查 protected-resource metadata，再查 authorization-server metadata；
- metadata issuer 必须精确匹配，resource metadata 若声明 resource 也必须等于当前 MCP URL；所有非 loopback endpoint 默认要求 HTTPS；
- 没有配置 client_id 时使用 metadata 的 registration endpoint 动态注册，并安全保存返回的 client information；
- authorization request 带 `response_type=code`、state、PKCE S256、redirect URI、scope 和 RFC 8707 resource；
- callback 只监听 `127.0.0.1`，exact path，拒绝缺失/重复/错误 state、空或超长 code；错误文本 HTML escape 且响应 `no-store`；
- token exchange 支持 `client_secret_post` 或 public client；严格接受 Bearer、非空 access token、有限正 expires_in；
- refresh 保留服务未轮换时的旧 refresh token；同进程不同 Agent/manager 按 store+name+URL single-flight，避免旋转 token 被并发消费两次；
- 凭据按 MCP name 和 exact server URL 绑定；默认文件不在项目目录，而是 `~/.nz-coder/oauth/mcp-auth.json`；显式
  `NZ_MCP_AUTH_STORE` override 的位置由用户负责；
- credential directory/file 分别要求 `0700/0600`，拒绝 symlink、非 regular file、过宽权限和超过 1 MiB 的内容；用同目录 `0600`
  临时文件、fsync 和 atomic replace 写入；
- OAuth-enabled server 的 runtime 自动读取有效 token，临近过期时 refresh；401 进入 `needs_auth`，退休 live binding，并删除失效 token、
  保留 dynamic client；`oauth:false` 的静态 bearer 失败仍是普通 `failed`；
- 对 OAuth-enabled server，`nz-coder mcp auth SERVER` 打印 URL并 best-effort 打开浏览器，`status` 只输出
  authenticated/expired/not_authenticated，`logout` 删除该 server 的全部凭据；`oauth:false` 会明确拒绝这些 OAuth 命令；三条命令均不打印
  token/client secret；
- pending flow 是进程内对象；timeout、取消和完成都会关闭 callback server，清空 verifier/code 引用。

配置示例：

```bash
export NZ_MCP_ENABLED=1
export NZ_REMOTE_MCP_CLIENT_SECRET='replace-me'
export NZ_MCP_SERVERS_JSON='{
  "servers": {
    "remote": {
      "type": "remote",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "client_id": "registered-client",
        "client_secret_env": "NZ_REMOTE_MCP_CLIENT_SECRET",
        "scope": "tools.read"
      }
    }
  }
}'
nz-coder mcp auth remote
nz-coder mcp status remote
```

没有预注册 client 时可省略 `client_id/client_secret_env`，但 authorization server 必须提供 dynamic registration endpoint。

### 29.4 关键设计决策

#### 授权必须是用户显式 CLI 操作

普通 Agent 启动遇到 401 时只进入 `needs_auth`，不会自动绑定端口、打开浏览器或等待五分钟。交互副作用由用户显式执行
`nz-coder mcp auth`，授权成功后的 token 才由 runtime 自动使用。这避免 headless、SWE-bench 或 HTTP service 启动时意外阻塞。

#### Token store 是用户级而不是 workspace 文件

把 access/refresh token 写入 `.nz-coder/` 会增加误提交、workspace 工具读取和恶意仓库替换的风险。本实现默认写入用户目录下独立的 OAuth
目录，并把文件绑定 exact server URL；同名 server 改 URL 后不能读取旧 credential。

#### 不持久化 pending state/verifier

InfCode SDK/provider 会把 verifier/state 放入 auth document，用于 start/finish 两段 API。NZ-Coder 当前 CLI 在同一进程完成 begin/callback/exchange，
因此 pending secret 只留在 `PendingOAuth` 内存，取消或完成后清空。进程崩溃时用户重新授权，不留下可复用的半完成 flow。

#### 失败 token 与 dynamic client 分开失效

MCP 401 说明 access credential 已不可用，但不必重新动态注册 client。runtime 清除 tokens 并保留 URL-bound client information，下一次
`mcp auth` 可直接重新授权。

#### Refresh 需要 single-flight

多个 Agent 可以同时创建自己的 MCPRuntime。对会轮换 refresh token 的 server，两个并发 refresh 中第二个可能必然失败。本实现以
credential store path、server name 和 URL 为 key，在同一进程内串行 refresh，并在锁内重新读取已经更新的 token。

### 29.5 关键文件

- `nz_coder/mcp/oauth.py`：metadata、registration、PKCE/state、callback、exchange、refresh 和 authorization header；
- `nz_coder/mcp/auth_store.py`：URL-bound `0700/0600` store、bounded/no-follow read 和 atomic write；
- `nz_coder/mcp/config.py`：OAuth config schema、secret env 和 callback/endpoint 校验；
- `nz_coder/mcp/http_client.py`：401 分类为 `MCPAuthenticationRequired`；
- `nz_coder/mcp/runtime.py`：token 注入、refresh、`needs_auth` 和 rejected-token retirement；
- `nz_coder/mcp/cli.py`、`nz_coder/interface/cli.py`：auth/status/logout 入口；
- `.env.example`：静态 bearer 与 OAuth 两种配置；
- `tests/test_mcp_oauth.py`：真实 loopback OAuth/MCP server contract。

### 29.6 验证结果

- Ruff：MCP、CLI 与相关 tests `All checks passed!`；
- `py_compile`：`nz_coder/mcp/*.py` 和 `nz_coder/interface/cli.py` 通过；
- OAuth 定向：`13 passed`；
- MCP/OAuth/CLI 聚焦：`84 passed`；
- 完整回归：`724 passed, 3 warnings`；新增 `tests/__init__.py` 固定本项目 `tests.*` helper import，消除此前受导入顺序影响的 Dodo
  collection 故障；
- loopback contract 未 mock OAuth 网络：真实执行 discovery、registration、callback HTTP、code exchange、runtime MCP initialize 和 refresh；
- 安全覆盖错误 state 不消费 flow、cancel 唤醒、URL binding、0600/0700、symlink/过宽权限、inline secret/remote callback 拒绝、
  startup/mid-session rejected-token invalidation、stale 401 不删除新 token、终端控制字符过滤、CLI 不输出 token 和 8 manager refresh
  single-flight；
- 没有使用公网第三方 OAuth/MCP server、真实用户账号或真实 client secret；没有运行 SWE-bench。

复现命令：

```bash
python3 -m ruff check \
  nz_coder/mcp nz_coder/interface/cli.py \
  tests/test_mcp_oauth.py tests/test_mcp_http.py tests/test_mcp.py tests/test_cli_commands.py
python3 -m py_compile nz_coder/mcp/*.py nz_coder/interface/cli.py
python3 -m pytest -q tests/test_mcp_oauth.py
python3 -m pytest -q \
  tests/test_mcp_oauth.py tests/test_mcp_http.py tests/test_mcp.py tests/test_cli_commands.py
python3 -m pytest -q
```

### 29.7 学习重点

1. OAuth 支持不是“保存 bearer token”；discovery、PKCE、state、callback、exchange、refresh、失效和删除必须形成一个状态机。
2. state 防 callback CSRF，PKCE 防截获的 authorization code 被别的 client 使用；两者解决的问题不同，不能互相替代。
3. token 和 dynamic client 都必须绑定 resource/server URL，否则同名配置改地址后可能向错误服务发送 credential。
4. callback 应先成功绑定再交付 authorization URL，并只监听明确的 loopback IP；不能用 `0.0.0.0` 或远程 redirect。
5. 文件 mode 只是存储安全的一部分，还需要拒绝 symlink、限制大小、fd 后复核、原子替换和不在日志/CLI 输出 secret。
6. refresh token 可能轮换，因此并发 refresh 必须在锁内重读，不能只在网络请求周围加锁。
7. runtime 遇到认证失败应移除 stale binding 和失效 token，但保留仍可复用的 dynamic client registration。

### 29.8 剩余差距

- 没有从 MCP 401 `WWW-Authenticate` challenge 读取 `resource_metadata` 和 scope；当前主动尝试 well-known protected-resource URI；
- 不支持 OpenID Connect discovery fallback、多个 authorization server 选择、resource indicator negotiation 或 incremental consent；
- 只支持 authorization code + PKCE S256、Bearer 和 client_secret_post/none；不支持 DPoP、private_key_jwt、device flow 或 token introspection；
- dynamic registration 没有 registration access token、client update/delete 或软件声明；只执行 `none` 和 `client_secret_post` 两种已确认方法；
- refresh single-flight 只覆盖同一进程，没有跨进程文件锁；credential atomic replace 也没有 fsync parent directory；
- pending state/verifier 不持久化，因此不能跨进程执行 startAuth/finishAuth，进程退出后必须重新授权；
- callback 只支持固定 `127.0.0.1` HTTP，不支持自定义 HTTPS callback、IPv6 loopback 或 GUI deep link；
- 没有 token revocation endpoint；logout 只删除本地 credential；
- `needs_client_registration` 没有独立 runtime status，缺少 registration endpoint 时由 `mcp auth` 返回明确错误；
- 没有 HTTP Session API/SDK/GUI 的 MCP auth 管理端点，当前 consumer 只有本地 CLI；
- 没有 project/user MCP config merge、trust/reconcile、旧 SSE fallback 或第三方公网 interoperability smoke；
- OAuth HTTP timeout 仍是 socket inactivity timeout，不是 slow-drip 总墙钟 deadline。

## 30. 历史差距再审计（A028 时点，已由 A036 取代）

> 历史快照：本节只描述 A028 完成时的状态。A029–A035 已解决其中列出的持久索引、模型发现、MCP 配置与 reconcile、后台写 Agent、Dodo 收敛和扩展描述问题。当前能力与待办以第 39 节 A036 和 [`release-baseline.md`](release-baseline.md) 为准，不应再把本节表格当作当前 backlog。

### 30.1 审计口径

本次审计重新读取当前工作区中的 NZ-Coder 与本地 InfCode 源码，不直接沿用最早的差距表。判断分为四类：

- **核心已对齐**：关键生命周期已经闭环，剩余差异不会阻断终端 Agent 的主要工作流；
- **部分对齐**：已有可用主链路，但覆盖范围、协议兼容性或产品化程度仍明显小于 InfCode；
- **未对齐**：尚无对应的稳定 consumer 或交付形态；
- **证据未收口**：代码和流程存在，但缺少统一口径的可复现实验结果。

对比对象是当前本地 `infcode-dev/infcode-dev` 快照。此次只做源码与文档审计，没有发送真实模型请求、连接公网 MCP server，也没有按用户当前安排运行 SWE-bench。

### 30.2 对最早差距表的纠正

| 最早判断 | 当前结论 | 依据 |
|---|---|---|
| 运行时状态没有真正隔离 | **已解决，不再是 P0** | A014 已把 workspace、运行参数、文件事务依赖和动态工具状态迁移到 ContextVar/实例作用域；`runtime/workdir.py`、`runtime/execution_context.py`、`tools/files.py` 均不再靠临时改写模块级 workspace 工作。 |
| 模型适配层太薄 | **核心协议继续收窄，生态广度仍是差距** | A028 后已有 OpenAI-compatible、OpenAI Responses/Codex、Anthropic、Gemini 四类请求/流式归一化链路和模型 capability/variant；但 `providers/__init__.py` 中多数第三方 provider 名仍共用 Chat Completions 兼容层，尚无模型目录在线发现、model picker 和 InfCode 级 provider SDK 矩阵。 |
| 多语言代码理解明显偏弱 | **部分解决** | A001–A005 已增加 LSP、Python AST、多语言声明地图、增量指纹缓存和 LSP workspace symbols；但非 Python 结构仍主要由保守文本规则提取，尚无 Tree-sitter 分块、持久增量索引、embedding/vector retrieval 和 watcher 驱动更新。 |
| 评测证据尚未收口 | **仍成立，且按当前要求暂缓** | 历史文档中同时存在 30/49 与 27/37 两套未核实汇总口径，并混合了不同批次、人工定向 retry 与历史配置；在原始 predictions/reports 归档核验前，两者都不等于固定模型/配置下 300 个 Lite 实例的一次性官方结果。 |
| 上下文管理不够模型感知 | **核心已解决** | A006/A015 已按模型窗口预留输出、计算软阈值、持久化超长输入/工具输出、裁剪旧工具结果，并保留 anchored summary 与近期完整回合；不再是固定 100K 后整体压成一条摘要。 |
| 没有 MCP 与成熟插件协议 | **MCP 主链路已解决；插件生态仍部分缺失** | A016/A024/A026/A027 已覆盖 stdio、Streamable HTTP、动态 tools/prompts/resources、list-changed、OAuth 与 CLI 管理；但 project config reconcile、legacy SSE 和第三方互操作仍缺。NZ-Coder 有 skills、hooks、optional packs 和 MCP 动态工具，却没有 InfCode 那种可安装插件包及统一 hook 生命周期。 |
| 子 Agent 不是真正并行编排 | **部分解决** | ContextVar/worktree/路径 claim/冲突检测已经解决隔离问题；同一模型响应中的 explore/plan/reflection `task` 可进入只读并发段，但 write-capable general-purpose Agent 仍作为副作用屏障串行执行，也没有一次提交 1–20 个后台任务的 Agent Manager/宿主进度面板。 |
| 缺少统一客户端协议和 IDE 生态 | **核心 Session 协议已解决，客户端生态仍未对齐** | A017–A023 已形成 Session、message/part、permission/question、snapshot、SSE cursor、断流恢复和崩溃状态闭环；但没有生成式 SDK、GUI Bridge、VS Code/JetBrains host。现有 PySide 客户端仍属于 Dodo legacy，不是新 Session API 的正式 consumer。 |
| 工程范围发散 | **仍成立，但核心边界已改善** | Dodo 已退出默认 CLI、trace 和 memory 主链路，通用能力已内化到 core；然而 `nz_coder/dodo/`、`pyside_client/` 及其大量测试仍保留两套控制面与客户端概念，继续产生维护成本。 |

### 30.3 当前能力矩阵

| 维度 | 当前状态 | 与 InfCode 相比剩余的实质差距 |
|---|---|---|
| workspace/session/runtime 隔离 | **核心已对齐** | 仍缺跨进程级资源治理，但原先的模块级串 workspace 风险已消除。 |
| 上下文预算与压缩 | **核心已对齐** | token 计数仍以本地估算为主，缺少各 provider 返回 usage 的统一校准和更大真实模型矩阵。 |
| HTTP Session service | **核心已对齐并冻结** | 缺 SDK、IDE consumer、remote deployment/auth、配额和完整多媒体 Part；这些属于 App/平台范围，不是当前终端 Agent 的闭环缺陷。 |
| MCP | **核心协议已对齐，配置产品化部分对齐** | 缺 project/user 配置合并与信任审批、运行中增删改 reconcile、legacy SSE fallback、HTTP Session 管理端点和公网互操作矩阵。 |
| Provider/model | **部分对齐（A028 已补 Responses）** | 已有四种 adapter family，Codex/OpenAI Responses wire protocol 与专属凭据已落地；大量第三方名称仍只是 OpenAI-compatible 别名，缺在线 model discovery、model picker 和广泛 live smoke。 |
| 多语言代码理解 | **部分对齐** | 有 LSP 与轻量 Repo Map，但没有持久语义索引、文件 watcher、Tree-sitter 级稳定分块和向量检索；非 Python call graph 仍弱。 |
| 子 Agent 编排 | **部分对齐** | 只读子任务可并行，写 Agent 仍缺显式后台任务组、状态查询、批量取消和安全汇入父 workspace 的完整协议。 |
| Skills/hooks/plugins | **部分对齐** | skills、配置 hooks、optional packs、动态 MCP tools 均可用；缺插件包发现/安装、统一 config/event/auth/tool hook 接口和版本/信任边界。 |
| SDK/GUI/IDE | **未对齐** | InfCode 有 OpenAPI/JS SDK、GUI Bridge、VS Code 和 JetBrains；NZ-Coder 只有本地 HTTP client 与旧 Dodo PySide consumer。 |
| SWE-bench 证据 | **证据未收口** | 缺固定模型、固定配置、固定 commit、完整 300 Lite 的官方一次性结果及原始 predictions/report 可复现归档。 |
| Dodo legacy | **待收敛** | 核心已不依赖 Dodo，但物理代码与测试尚未删除或改造成新 Session service adapter。 |

### 30.4 现阶段优先级

#### P0：核心 Agent 能力差距

1. **代码理解从临时地图升级为持久增量索引**：优先利用现有 LSP、SQLite/标准库和文件指纹，建立可更新的 symbol/reference cache 与统一检索排序；受项目依赖约束，不应为了表面一致直接引入 Tree-sitter、LanceDB 或 Qdrant。
2. **SWE-bench 可复现证据**：这是面试展示和能力判断的 P0，但遵从当前“评测流程先不跑”的安排，标记为 **deferred P0**，不阻塞下一轮源码对齐。恢复条件是用户明确重新启动评测，或项目进入发布/面试材料定稿阶段；届时它应立即回到执行队列，而不是永久后置。

#### P1：运行时与扩展闭环

1. **Provider 模型发现与选择体验**：在 A028 原生协议上增加 opt-in model list discovery、缓存、能力覆盖和 CLI model picker；真实 live smoke 仍必须由用户明确授权。
2. **MCP 配置与互操作**：project/user config、local command trust、运行中 reconcile、legacy SSE fallback、第三方 server smoke。
3. **写子 Agent 的后台编排协议**：任务组状态、取消、超时、路径 claim、worktree 结果检查，以及把已审核补丁显式移植/应用到父 workspace；在这套协议完成前，不应只把 `task` 放进线程池就宣称“多 Agent 并行已对齐”。
4. **Dodo 物理收敛**：保留已经内化到 core 的能力；无调用方的 scheduler/ingress/hosted API/PySide 要么改造成新 Session service 的薄 adapter，要么删除，不能继续维护两套主架构。
5. **插件边界**：先把现有 skills、hooks、optional packs、MCP dynamic tools 统一成可描述的扩展 contract；只有出现真实第三方插件 consumer 后再增加安装、版本和 auth hook。

#### P2：产品生态差距

1. OpenAPI 代码生成 SDK、GUI Bridge、VS Code/JetBrains host；
2. remote workspace/control plane、账号同步、分享与云端能力；
3. 20 余种 provider 的全面 SDK 适配，以及 embedding/vector-store 多后端矩阵。

这些是 InfCode 作为完整产品的广度，不应自动升级为 NZ-Coder 终端 Agent 的近期必做项。若未来明确要做 App，再把 SDK/IDE consumer 提升优先级。

### 30.5 建议实施顺序

在继续暂缓 SWE-bench 的前提下，推荐顺序为：

```text
持久增量代码索引与统一检索
  -> Provider model discovery/picker
  -> MCP project config/trust/reconcile
  -> write-capable 子 Agent 后台编排
  -> 统一现有扩展 contract
  -> Dodo 物理收敛
  -> 有明确 consumer 时再做插件打包、SDK/IDE
```

这里最重要的收敛原则是：**HTTP、MCP、LSP 现在都已有核心闭环，后续应补最短的缺失链路，不再因 InfCode 某个目录更大就复制整套产品面。**

### 30.6 关键证据文件

- NZ-Coder runtime：`nz_coder/runtime/workdir.py`、`nz_coder/runtime/execution_context.py`、`nz_coder/tools/files.py`；
- NZ-Coder provider/context：`nz_coder/providers/`、`nz_coder/state/context.py`、`nz_coder/runtime/loop.py`；
- NZ-Coder code intelligence：`nz_coder/lsp/`、`nz_coder/tools/repo_map.py`、`nz_coder/tools/repo_intel.py`；
- NZ-Coder Session/MCP：`nz_coder/http_service/`、`nz_coder/session_events.py`、`nz_coder/mcp/`；
- NZ-Coder extensions/subagents：`nz_coder/state/skills.py`、`nz_coder/runtime/hooks.py`、`nz_coder/runtime/subagent.py`；
- InfCode 对照：`packages/opencode/src/provider/provider.ts`、`session/context-budget.ts`、`session/compaction.ts`、`lsp/`、`mcp/index.ts`、`plugin/index.ts`、`kilocode/tool/agent-manager.ts`；
- InfCode 产品 consumer：`packages/sdk/`、`packages/gui-bridge/`、`apps/vscode/`、`apps/jetbrains/`、`packages/kilo-indexing/`。

### 30.7 验证边界

- 本节是差距审计，不是 A028 能力实现，因此没有在总览中伪造“已完成”条目；
- 逐项读取了当前本地双方源码，并用符号/调用点搜索核对是否存在实际 consumer；
- 没有修改运行时代码，沿用 A027 完成时的最新完整回归基线：`724 passed, 3 warnings`；
- 没有运行 SWE-bench；历史资料中的 30/49 与 27/37 两套口径仅用于证明“证据尚未统一”，在核验原始 reports 前均不作为当前版本官方得分。

## 31. A028：OpenAI Responses/Codex 原生 Provider

### 31.1 InfCode 参考能力与官方契约

本项同时参考两类来源：

- 本地 InfCode：`packages/opencode/src/provider/provider.ts`、`provider/transform.ts`、`session/system.ts`，用于核对 provider SDK 路由、模型能力与 prompt family 的分层方式；
- OpenAI 官方 Responses API：`https://platform.openai.com/docs/api-reference/responses` 与 `responses-streaming`，用于核对 `input` item、function tool、`function_call_output`、`response.output_text.delta`、`response.function_call_arguments.delta` 等 wire contract。

InfCode 的优势不是把所有模型伪装成同一种 HTTP 请求，而是根据 provider/model 选择 SDK、参数和 prompt。NZ-Coder 因此也不能仅把 `codex` 加成一个 OpenAI-compatible 别名；Responses 的 history、tool result 和 streaming event 都与 Chat Completions 不同。

### 31.2 NZ-Coder 原有不足

A025 之前已经有精确模型目录和 reasoning variants，但 OpenAI/Codex 实际仍只能走：

```text
client.chat.completions.create(...)
  -> choices[0].message / choices[0].delta
```

这会遗漏 Responses 的核心语义：

- history 是 message、function call、function call output 和 reasoning item 的序列；
- 工具定义不是 Chat Completions 的嵌套 `function` 形状；
- 流式工具参数通过独立 event type 传输；
- `store:false` 手动管理历史时，需要保留 encrypted reasoning item；
- OpenAI 凭据和 endpoint 不应继续复用 DashScope 等通用兼容配置作为默认值。

### 31.3 实现结果

新增 `OpenAIResponsesProvider`，仅在 `MODEL_PROVIDER=openai-responses`、`openai_responses` 或 `codex` 时启用。原有 OpenAI-compatible、Anthropic、Gemini 路径不变。

核心调用链：

```text
AgentLoop 的统一 messages/tools/max_tokens
  -> OpenAIResponsesProvider
  -> Chat history 转 Responses input items
  -> client.responses.create(...)
  -> Responses output/event 归一化
  -> NormalizedCompletion / NormalizedChunk
  -> 既有 AgentLoop、tool executor、subagent、compact 继续复用
```

已经闭合：

- system/developer/user/assistant message 转换；
- assistant function call 与 tool `function_call_output` round-trip；
- Chat tool schema/tool choice 转 Responses 扁平 function schema；
- `max_tokens` 到 `max_output_tokens`；
- `response.output_text.delta`、reasoning summary、function-call added/arguments delta/done；
- 非流式 message/function call/reasoning summary 归一化；
- `store:false` 加 `reasoning.encrypted_content` 请求，并将 replay-safe reasoning item 保存到 assistant message/tool metadata；
- reasoning variant 的 `reasoning_effort` 转 Responses `reasoning.effort`；
- failed/incomplete/error event 显式失败，不把半响应伪装成成功；
- `OPENAI_API_KEY`、`OPENAI_API_BASE_URL` 专属配置，默认 endpoint 为 `https://api.openai.com/v1`。

### 31.4 关键设计决策

1. **独立 adapter，不修改兼容层**：Responses 与 Chat Completions 是不同协议。独立路由既保护当前 Qwen/DeepSeek 等配置，也让面试时能清楚解释 adapter boundary。
2. **继续归一化到既有 Loop contract**：AgentLoop 不需要出现 provider-specific event 分支；协议差异由 adapter 吸收。
3. **reasoning 保存到 message 级元数据**：只挂在 tool call 上会漏掉“文本回复后 verification gate 要求继续”的回合。message/tool 两处都可携带，重放时按 reasoning ID 去重。
4. **默认 `store:false`**：会话历史由 NZ-Coder 自己持久化；同时请求 encrypted reasoning，保证 reasoning model 的工具回合能手动续接。
5. **未知 Responses 参数立即拒绝**：避免把 Chat Completions 参数静默发到另一协议后得到难定位的 400。

### 31.5 关键文件

- `nz_coder/providers/openai_responses.py`：输入转换、请求参数、非流式/流式归一化和 reasoning replay；
- `nz_coder/providers/normalized.py`：message/delta 级 provider metadata；
- `nz_coder/providers/__init__.py`：Responses/Codex 显式 provider 路由；
- `nz_coder/config.py`、`.env.example`：OpenAI 专属 credential/endpoint；
- `nz_coder/runtime/loop.py`：在统一 LLMResult 中保留 message 级 provider metadata；
- `tests/test_openai_responses.py`：离线 wire contract 与 AgentLoop 累计测试。

### 31.6 验证结果

- `py_compile`：新增 provider、配置、注册和测试通过；
- Ruff：相关 provider/runtime/tests `All checks passed!`；
- Responses 定向测试：`7 passed`；
- Provider/Loop/Context/Subagent 聚焦回归：`129 passed`；
- 完整回归：`731 passed, 3 warnings`；
- 使用当前安装的官方 `openai 2.36.0` 和本地 MockTransport 执行真实 SDK 序列化，确认 `input`、`max_output_tokens`、`store:false` 和 reasoning include 能进入 `/responses` 请求；
- 没有读取真实 API key、没有发送计费请求、没有运行 SWE-bench。

### 31.7 学习重点

1. Provider adapter 的价值是隔离 wire protocol，不只是保存不同的 base URL。
2. Responses 的 function call 是 output item，工具结果是下一个 input item；不能照搬 Chat 的 assistant/tool message JSON。
3. 流式 event 是按类型分发，function name、call ID、arguments delta 和 reasoning item 必须按 output index 合并。
4. 手动管理 reasoning model 历史时，encrypted reasoning item 是续接状态的一部分。
5. Provider-specific metadata 必须跟随持久消息，而不能只存在 adapter 的进程内字段。

### 31.8 剩余差距

- 没有在线 model list discovery、缓存和 CLI model picker；
- 没有真实 OpenAI/Codex credential live smoke，本项只验证官方 SDK 的本地序列化；
- 只覆盖 text、自定义 function tool 和 reasoning replay，未支持图片/文件输入、built-in web/file/code tools、remote MCP tool 或 programmatic tool calling；
- 没有使用 `previous_response_id`/Conversation API；当前明确选择 `store:false` 加手动完整历史；
- 未把 Responses usage/cache token 统一接到 trace/cost 统计；
- 兼容第三方“类 Responses”endpoint 不是本项承诺，显式 provider 当前按 OpenAI 官方 contract 实现。

## 32. A029：workspace 持久增量代码索引

### 32.1 InfCode 参考能力

本项重新阅读了本地 InfCode 的 `packages/kilo-indexing/src/indexing/`，重点是 scanner、cache manager、parser、file watcher、service factory 和 vector-store 边界。

InfCode 的完整索引链路会扫描 workspace，以文件 hash 判断新增/修改/删除，通过 Tree-sitter 提取结构，借助 watcher 持续更新，并可把 chunk embedding 保存到 LanceDB 或 Qdrant。它的核心价值不是某个搜索工具，而是让扫描、缓存、增量变更和查询共享同一份持久状态。

### 32.2 NZ-Coder 原有不足

A002–A005 已有多语言 Repo Map 和 LSP 补充，但结构缓存只是 `repo_map.py` 内的模块级字典：

- 进程退出后索引全部丢失；
- Repo Map 与引用/调用查询各自解析源码；
- 写工具提交后不会主动更新结构缓存；
- 删除文件只能等下一次对应范围扫描才被发现；
- 缓存键和数据都存在进程全局，不适合作为明确的 workspace service 边界。

因此原实现更接近“带进程内缓存的扫描命令”，还不是“首次扫描—增量更新—查询—重启复用”的索引闭环。

### 32.3 实现结果

新增标准库 SQLite 持久索引，每个 workspace 独立保存在 `.nz-coder/index/code-index.sqlite3`。

核心调用链：

```text
首次 repo_map / code_references
  -> 安全解析 workspace 与扫描范围
  -> 比较 path + mtime_ns + size
  -> Python AST / 多语言保守声明提取
  -> files + symbols + refs 原子写入 SQLite

后续进程或 Session
  -> 打开同一 workspace 数据库
  -> 未变文件直接复用
  -> repo_map 从统一索引加载 symbols

成功写事务 commit
  -> 汇总工具参数与 ChangeTracker 的修改/删除路径
  -> 单文件 replace/delete
  -> 再执行既有 patch-risk 与 LSP 写后诊断链路
```

已经闭合：

- workspace 级数据库路径和符号链接逃逸检查；
- `files`、`symbols`、`refs` 三表以及查询索引、foreign-key cascade、WAL 和 schema version；
- Python 顶层类/函数/方法与 `Name`/`Attribute` load reference；
- 十个已有非 Python 语言族的声明复用，避免另造一套语言识别规则；
- Repo Map 重启后持久 cache hit、`refresh=True` 强制重建；
- 完整扫描时清理 stale row，`max_files` 截断时禁止误删未访问文件；
- 新增安全只读工具 `code_references`，按 source path/line/column 返回精确 Python identifier 使用点；
- 同步和异步工具批次在事务成功提交后增量刷新，回滚批次不刷新；
- 写工具参数未枚举所有生成文件时，以 ChangeTracker 当前修改/删除集合补齐。

### 32.4 关键设计决策

1. **SQLite 而不是新增向量数据库**：当前面试优先级是展示索引一致性、持久化和增量更新。SQLite 属于标准库部署边界，符合 NZ-Coder 不引入新依赖的约束。
2. **Repo Map 迁移到统一索引，而不是新增孤立工具**：如果旧 Repo Map 继续维护另一份缓存，就没有真正形成 shared indexing service。
3. **写后更新只发生在 commit 之后**：在事务执行中提前更新会把随后 rollback 的内容写进索引；索引失败只记 trace，不反向破坏已经提交的用户文件。
4. **缓存文件不纳入文件事务**：它是可重建派生数据，不应污染用户 patch 或 ChangeTracker；数据库目录仍严格限制在 workspace 内。
5. **只为 Python 建 reference 表**：现有标准库 AST 能提供可信 identifier 位置；对非 Python 使用正则猜引用会制造大量假阳性，语义引用继续交给已安装 LSP。
6. **截断扫描不做全范围 stale delete**：没有访问到的路径不能被解释为文件已删除，这是增量索引最容易出现的数据丢失错误之一。

### 32.5 关键文件

- `nz_coder/intelligence/code_index.py`：SQLite schema、扫描、解析、持久加载、引用查询和单路径增量更新；
- `nz_coder/tools/repo_map.py`：Repo Map 迁移到持久索引并注册 `code_references`；
- `nz_coder/runtime/loop.py`：成功事务提交后的 best-effort 索引刷新；
- `nz_coder/tool_platform/permissioning/tool_groups.py`、`nz_coder/runtime/subagent.py`、`nz_coder/runtime/runtime_state.py`、`nz_coder/runtime/prompt.py`：权限、子 Agent、运行态和提示词接入；
- `tests/test_code_index.py`、`tests/test_repo_map.py`：持久、增量、删除、安全、截断和工具集成测试。

### 32.6 验证结果

- `py_compile`：索引、Repo Map 和 Loop 通过；
- Ruff：相关 intelligence/tools/runtime/tests `All checks passed!`；
- 索引定向测试：`7 passed`；
- Code Index/Repo Map/LSP/多语言/排序/Runtime State/Subagent/Loop 聚焦回归：`105 passed`；
- 完整回归：`738 passed, 3 warnings`；
- 没有运行 SWE-bench，符合当前“先对齐功能、不跑评测流程”的约定。

### 32.7 学习重点

1. 增量索引的难点是缓存一致性，不是把 AST 结果写入数据库。
2. workspace identity 必须进入持久路径；否则并发 Session 即使 SQL 正确，也会查询到错误项目的数据。
3. 写事务、派生缓存和 LSP 诊断有不同的失败语义：用户文件 commit 是主结果，索引与诊断都应 best-effort 跟随。
4. 删除与截断必须分开处理；“本轮没扫到”不等于“文件不存在”。
5. 语法索引和语义索引互补：SQLite/AST 提供零依赖、可重启的基础层，LSP 提供跨语言解析后的定义与引用。

### 32.8 剩余差距

- 没有常驻 filesystem watcher；Agent 自己的写入会立即更新，外部编辑要等下一次 Repo Map/引用查询扫描；
- cache hit 使用 `mtime_ns + size`，尚未增加 InfCode 式内容 hash 校验；
- 非 Python 只保存声明，不保存引用；跨语言语义引用仍依赖 LSP；
- 没有 Tree-sitter 增量语法树、chunk、embedding、LanceDB/Qdrant 或语义向量召回；
- `code_references` 是精确 identifier 查询，不等同于类型解析后的 compiler-grade references；
- schema 目前只有 version 1 标记，尚无未来版本的迁移器；缓存可删除后重建。

## 33. A030：写子 Agent 后台并行编排

### 33.1 InfCode 参考能力

本项重新核对了本地 InfCode 的 `packages/opencode/src/kilocode/tool/agent-manager.ts`。该工具接收 1–20 个任务、`local/worktree` 模式和 versions 标记，发布 `AgentManagerEvent.Start` 后立即返回 request ID；实际 Session 创建和进度卡由 VS Code 扩展消费事件完成。

因此 InfCode 当前源码中的 Agent Manager 并不是核心 runtime 内部的线程池或补丁合并器，而是“core 发出异步创建请求，宿主负责后台 Session”的产品架构。NZ-Coder 目前没有对应 IDE host，本项对齐的是可验证的运行语义：批量异步启动、隔离、状态管理和父任务显式接收结果，而不是复制一个没有 consumer 的 VS Code event。

### 33.2 NZ-Coder 原有不足

A030 前的 `task` 已经具备很好的基础：

- child Session 和独立 trace；
- `general-purpose` 的 worktree；
- `target_paths` claim、active overlap 阻断和完成后 changed-file 冲突报告；
- 子 Agent 内部事务、失败回滚和轻量验证；
- `message_parent` 暂停/恢复。

但调用仍会同步占住父 Agent，且存在三个闭环缺口：

1. 多个写子任务不能作为一个后台任务组受控启动、查询或取消；
2. 非 Git workspace 会退化为 `direct`，并行写时实际仍可能修改同一个父目录；
3. child worktree 结果没有统一的“父 Agent 已审查—父目录未漂移—显式应用”入口。

### 33.3 实现结果

新增 Session-owned `BackgroundAgentManager`，每个 workspace/Session identity 在进程内只有一个 owner，运行状态继续落在原有 child `state.json` 中。

核心调用链：

```text
agent_manager(action=start, tasks=[...])
  -> 校验 1–20 个任务、prompt 和非空 target_paths
  -> 与同批任务及现有 active child 做路径前缀冲突检查
  -> 记录父 workspace baseline SHA-256 manifest
  -> 原子写 queued child state
  -> daemon worker + bounded semaphore（默认并发 4）
  -> general-purpose run_subagent
  -> Git worktree 或隔离 copy snapshot
  -> completed / needs_parent / cancelled / timeout / error 持久状态

agent_manager(action=status|cancel)
  -> 查询持久任务状态、结果和 changed_files
  -> cooperative cancel_event
  -> 子 Agent 在模型调用边界回滚未提交修改并 settled

父 Agent逐文件检查 child worktree
  -> apply_agent_changes(confirm=true, reviewed_files=精确全集)
  -> claimed scope 检查
  -> parent current hash == spawn baseline hash
  -> child symlink/binary/数量限制检查
  -> 复用父 Agent 当前 TransactionManager 写入或删除
  -> 后续工具失败时仍可整批 rollback
```

已经闭合：

- 最多 20 个任务、默认最多 4 个同时调用模型，均可通过环境变量收紧；
- 启动前完整验证后再统一 reservation，不会出现半批任务已启动、后半批才发现 overlap；
- queued/running/cancel_requested/needs_parent/completed/cancelled/timeout/error/interrupted 等可持久查询；
- 同进程多个 AgentLoop 会复用同一 Session manager，不会把仍运行的任务误判为中断；
- 新进程发现遗留 queued/running/cancel_requested 状态时标记 `interrupted`；
- child 状态使用同进程锁和临时文件 replace，避免并发查询读到半截 JSON；
- Git 可用时 worktree 会覆盖成父目录启动时的当前文件快照，能看到父目录未提交修改；
- Git 不可用或 worktree 创建失败时使用 `.nz-coder/worktrees/<child>` 文件快照，不再退化为共享父目录；
- copy snapshot 跳过 VCS、NZ 状态、依赖和构建缓存目录，并拒绝源符号链接；
- worktree、subagent artifact 和 apply 路径均验证不逃逸 workspace；
- `apply_agent_changes` 必须收到与 child `changed_files` 完全一致的 `reviewed_files` 和 `confirm=true`；
- 父目录自 child 启动后发生同路径改变时拒绝覆盖；
- child 越过 claimed scope、符号链接、二进制文件和超过 50 个文件时拒绝自动应用；
- 应用复用父 Loop 的事务，后续同批工具失败仍会回滚。

### 33.4 关键设计决策

1. **保留 `task` 作为前台单子 Agent，新增 `agent_manager` 管任务组**：两者交互语义不同；把 start/status/cancel 塞进原 `task` 会破坏已有 resume contract。
2. **不把 Git 当成运行时数据库**：Git worktree 是优先隔离机制，但不是功能前提；无 Git 时使用标准库文件快照。这也回答了“为什么非要 Git”——现在不需要，只要能提供等价的目录隔离。
3. **后台写任务必须声明 target_paths**：没有路径 ownership 就无法在启动前证明并行安全，因此不允许依靠 prompt 猜测写范围。
4. **不自动 merge/apply**：子 Agent 完成只表示候选修改完成。父 Agent必须阅读 changed files，并显式提交精确清单；这是并行速度和代码所有权之间的安全边界。
5. **baseline hash 比“完成时比较 siblings”更重要**：即使子任务彼此不冲突，父 Agent自己也可能在 child 运行期间修改同一文件。spawn-time SHA-256 能阻止旧快照覆盖新工作。
6. **cooperative cancellation**：Python 线程无法安全杀死正在进行的 SDK 网络调用；cancel 会立即持久化 request，并在排队或模型调用返回边界退出、回滚。伪装成即时强杀反而会产生幽灵写入。
7. **核心 manager 取代宿主 event consumer**：InfCode 把执行交给 VS Code；NZ-Coder 尚无该宿主，因此由 Session core 持有 lifecycle，CLI/HTTP 都能复用，而不是增加一个无人消费的事件。

### 33.5 关键文件

- `nz_coder/runtime/agent_manager.py`：Session manager、并发槽、start/status/cancel、baseline 校验和显式 apply；
- `nz_coder/runtime/subagent.py`：queued/cancel 状态、原子 state、copy worktree 接受、冲突和变更文件过滤；
- `nz_coder/runtime/worktree/manager.py`：父目录当前状态同步和无 Git copy snapshot；
- `nz_coder/runtime/loop.py`：manager 生命周期所有权和 ContextVar 绑定；
- `nz_coder/runtime/prompt.py`、`nz_coder/tool_platform/permissioning/tool_groups.py`：使用规则与权限分类；
- `nz_coder/config.py`、`.env.example`：任务数和并发上限；
- `tests/test_agent_manager.py`、`tests/test_subagent.py`：并行、overlap、取消、中断、无 Git 隔离、路径安全、审查应用、父漂移和事务回滚。

### 33.6 验证结果

- `py_compile`：manager、subagent、worktree 和 Loop 通过；
- Ruff：相关 runtime/config/tests `All checks passed!`；
- Agent Manager 定向测试：`9 passed`；
- Manager/Subagent/Loop/Permission/Runtime Context/HTTP 聚焦回归：`121 passed`；
- 完整回归：`747 passed, 3 warnings`；
- 测试使用离线 fake child/fake completion，没有发起真实模型请求；
- 没有运行 SWE-bench。

### 33.7 学习重点

1. 并行写 Agent 的核心不是 `ThreadPoolExecutor`，而是 ownership、隔离、settlement 和结果接收协议。
2. queued 状态也必须占用路径 claim，否则两个线程可能在各自写入 state 前同时通过冲突检查。
3. worktree 隔离只解决 child 之间的文件系统竞争；baseline hash 才解决 parent 与 child 的时间漂移。
4. 后台状态文件必须原子 replace，否则 status 恰好读取写入中的 JSON 时会把真实任务暂时“丢失”。
5. “完成”和“已应用”是两个不同状态。父 Agent必须保留最终代码所有权。
6. Git 是一种高效的 snapshot/worktree 实现，不应该成为非 Git 项目无法使用 Agent 编排的理由。

### 33.8 剩余差距

- 没有 InfCode VS Code Agent Manager card、版本对比 UI 或 IDE session tabs；
- 线程中的在途模型请求只能 cooperative cancel，不能立即终止底层 socket；
- `needs_parent` 后仍通过原 `task(session_id=...)` 恢复，manager 暂无独立 message/resume action；
- copy snapshot 对超大 monorepo 成本高，尚无 reflink、增量 snapshot 或 scope-aware dependency copy；
- completed/cancelled worktree 尚无自动归档、清理和保留期限；
- 自动 apply 只支持最多 50 个 UTF-8 普通文件，binary、symlink 和超大批次要求人工处理；
- 没有任务优先级、动态并发调节、跨进程 worker 或远程执行；
- 没有自动合并、cherry-pick 或冲突解决，这是当前刻意保留的父 Agent 审查边界。

## 34. A031：MCP 分层配置、信任与 live reconcile

### 34.1 InfCode 参考能力

本项重新核对了本地 InfCode 的 `packages/opencode/src/mcp/index.ts` 和相关 config 类型。InfCode 会从统一配置系统取得 MCP 定义，为 local/remote server 建立状态；remote 连接先尝试 `StreamableHTTPClientTransport`，失败后尝试旧 `SSEClientTransport`，同时维护 connected/failed/needs_auth 等状态并向 Session 提供 tools、prompts 和 resources。

NZ-Coder 的 A024–A027 已经实现 stdio、Streamable HTTP、动态能力刷新和 OAuth，但配置来源仍只有 `NZ_MCP_SERVERS_JSON`。因此本项重点不是再写一种 JSON-RPC 调用，而是把“配置来源—本地命令信任—运行中变更—transport fallback”接成闭环。

### 34.2 NZ-Coder 原有不足

A031 前存在以下实际问题：

- 用户必须把所有 server 塞进单个环境变量，不支持个人配置和项目配置共存；
- 项目若携带 local MCP command，没有独立的执行信任步骤；
- AgentLoop 创建 MCPRuntime 后，配置文件增删改不会影响当前 Session；
- 配置改坏时没有“保留上一健康 generation”的策略；
- remote 只有 Streamable HTTP，无法连接仍使用旧 endpoint/message SSE 协议的 server；
- CLI 只能处理 OAuth，不能查看合并来源或管理 local command trust。

### 34.3 实现结果

配置加载顺序固定为：

```text
~/.config/nz-coder/mcp.json（user）
  -> <workspace>/.nz-coder/mcp.json（project，同名 server 整体替换）
  -> NZ_MCP_SERVERS_JSON（environment，同名 server 整体替换）
```

其中路径可通过 `NZ_MCP_USER_CONFIG`、`NZ_MCP_PROJECT_CONFIG` 和 `NZ_MCP_TRUST_STORE` 调整；project config 必须是 workspace-relative，解析后的真实路径不能逃逸 workspace。

local project command 的生命周期：

```text
读取 project server
  -> 对 name + command + resolved cwd + environment 计算 SHA-256
  -> 查询用户侧 0600 mcp-trust.json
  -> 未命中：status=untrusted，不启动进程、不暴露工具
  -> nz-coder mcp trust <server>
  -> 写入 workspace/server/fingerprint trust record
  -> 当前 Runtime 下一次 poll 发现 revision 改变
  -> reconcile config changed
  -> connect 并动态暴露 tools

command/cwd/env 任一变化
  -> fingerprint 改变
  -> 原 generation 关闭
  -> 新定义回到 untrusted
```

运行中 reconcile：

```text
tool_bindings/status_summary
  -> 比较 user/project/env/trust revision
  -> 配置未变：直接返回当前 cache
  -> 配置变化：重新严格校验
       -> invalid：记录 $config failed，保留当前健康 clients/bindings
       -> valid：计算 added/removed/changed
           -> retire 旧 live/pending generation
           -> 原子替换 configs/status/cache
           -> active Runtime 自动连接新增/改变且 enabled+trusted 的 server
```

远程 transport 链路：

```text
Streamable HTTP initialize
  -> 成功：沿用 A026 Session-ID/POST/GET/DELETE
  -> authentication required：保留 needs_auth，不用 fallback 掩盖
  -> 其他协议/HTTP失败：尝试 legacy SSE
       GET event stream
       -> endpoint event（只接受 same-origin URL）
       -> POST JSON-RPC 到 endpoint
       -> message event 关联 request ID
       -> tools/prompts/resources 与通知继续复用 MCPClient contract
```

CLI 新增：

- `nz-coder mcp list`：只显示 server 名、transport、source、enabled 和 trust 状态，不输出 command、env、header 或 token；
- `nz-coder mcp trust <server>`：仅允许信任 project-local stdio command 的当前 fingerprint；
- `nz-coder mcp untrust <server>`：删除对应用户 trust record；
- 原有 `auth/status/logout` OAuth 命令保持兼容。

### 34.4 关键设计决策

1. **同名 server 整体替换，不做字段 deep merge**：command 来自 user、env 来自 project 之类的隐式拼装难以审计，也会让 trust fingerprint 的含义不清晰。
2. **user/environment local command 默认可信，project local command 必须外部授权**：user 文件和进程环境属于操作者配置；project 文件属于工作区内容，可能来自刚下载的仓库，不能仅因打开项目就执行。
3. **trust record 放在用户路径，不放项目目录**：若项目能够同时修改 command 和“已信任”标记，信任机制等于不存在。
4. **fingerprint 包含 cwd 和 environment**：风险不只来自 executable；改变工作目录或注入环境变量也可能改变命令行为。
5. **轮询 revision 而不是新增常驻文件 watcher**：动态 tool provider 和状态查询本来就是 Session 的消费边界；mtime/size 检查成本低，也避免新增 watcher 线程和第三方依赖。
6. **invalid config 不摧毁 live generation**：配置编辑通常经历短暂的不完整 JSON；先验证新快照、成功后再 reconcile，可以避免保存到一半时所有 MCP 工具突然消失。
7. **legacy endpoint 必须 same-origin**：SSE `endpoint` 是服务端提供的数据，不能允许它把 Authorization/header credential 引向其他主机。
8. **认证错误不盲目 fallback**：Streamable HTTP 已明确返回 401 时，真实状态是 `needs_auth`；用随后 SSE 的 404 覆盖它会误导用户。

### 34.5 关键文件

- `nz_coder/mcp/config.py`：三层配置、source/trust metadata、项目路径安全、revision 和 command fingerprint；
- `nz_coder/mcp/trust.py`：用户侧 0600 原子 trust store；
- `nz_coder/mcp/runtime.py`：live config poll/reload/reconcile、generation retirement 和 transport fallback；
- `nz_coder/mcp/sse_client.py`：无代理、无重定向、same-origin、bounded legacy SSE client；
- `nz_coder/mcp/cli.py`：list/trust/untrust 与既有 OAuth CLI 路由；
- `nz_coder/config.py`、`.env.example`：配置路径和示例；
- `tests/test_mcp_config_reconcile.py`：merge、trust invalidation、reconcile、保活和 CLI；
- `tests/test_mcp_sse.py`：真实 loopback legacy SSE initialize/list/call/close fallback。

### 34.6 验证结果

- `py_compile`：config/trust/runtime/CLI/SSE client 通过；
- Ruff：完整 `nz_coder/mcp` 和相关测试 `All checks passed!`；
- 新增配置与 fallback 定向测试：`7 passed`；
- MCP/CLI/Loop/Subagent/HTTP 聚焦回归：`185 passed`；
- 完整回归：`754 passed, 3 warnings`；
- 真实协议冒烟一：启动独立 stdio fixture 子进程，验证 project untrusted 不启动、trust 后 initialize/list/call、command 改变后关闭并重新变为 untrusted、配置删除后移除；
- 真实协议冒烟二：启动独立 loopback HTTP Server，先让 Streamable POST 失败，再通过 legacy endpoint/message SSE 完成 initialize、tools/list、tools/call 和关闭；
- 没有使用真实 credential、没有访问公网、没有运行 SWE-bench。

### 34.7 学习重点

1. MCP client 写完并不代表 MCP 集成完成；配置 ownership 和进程执行信任同样属于协议边界。
2. 项目配置是数据，不应自动获得执行本地 command 的权限。
3. live reconcile 必须区分 desired config 和 accepted generation，新配置验证失败不能污染旧状态。
4. tool binding 是运行时能力快照；server 删除或 command 变更时必须同步退休 client、cache 和公开工具。
5. Streamable HTTP 的 SSE 是响应/通知格式；legacy SSE 的核心则是 GET stream 下发 POST endpoint，两者不能只改一个 Content-Type 就复用。
6. fallback 顺序会影响错误语义，最有诊断价值的 auth error 必须保留。

### 34.8 剩余差距

- revision 使用路径 `mtime_ns + size` 和环境 JSON hash，极端情况下外部工具保留相同 metadata 的改写要等显式 reload 或下一次 metadata 变化；
- 没有常驻 watcher，只有 tool schema/status 消费时触发 poll；
- user/project 同名配置采用整体替换，不支持显式字段继承；
- legacy SSE 没有 Last-Event-ID 重连或断流续传，transport 失败后由 Runtime 标记 failed，需 reconnect；
- trust store 是本机用户级 JSON，没有组织签名、策略分发或 command publisher identity；
- 尚未对公网第三方 MCP 服务发起 live smoke；本项只使用无 credential 的独立 stdio 子进程和 loopback 协议服务；
- project remote server 不需要 command trust，因为它不执行本机进程，但其工具仍按 external/untrusted output 和 PermissionManager 约束；
- 没有 GUI 配置编辑器、server marketplace 或 IDE 状态面板。

## 35. A032：Provider 模型发现、缓存与 workspace picker

### 35.1 InfCode 参考能力

- `packages/opencode/src/provider/models.ts`：从 models.dev 读取带模型能力的 provider catalog，使用磁盘缓存、内置 snapshot、5 分钟 freshness 和跨进程文件锁；
- `packages/opencode/src/provider/model-cache.ts`：对可动态获取模型的 provider 做 cache-first 加载和强制刷新；
- `packages/opencode/src/provider/provider.ts`：把静态 catalog、配置模型和 provider `discoverModels` 结果合并成运行时 provider/model 集合；
- `packages/opencode/src/cli/cmd/models.ts`：列出模型，并允许显式刷新模型缓存。

核心不是“在终端打印一串 ID”，而是让模型发现、能力解析、用户选择和实际 Session 使用同一份有效状态。

### 35.2 NZ-Coder 原有不足

A025 已能对一个已知 `provider/model` 解析能力和 reasoning variant，但存在四个断点：

- 不能向 Provider 查询当前账号实际可用的模型；
- 没有模型列表缓存，每次做 picker 都只能依赖手写配置；
- CLI 没有 `list/refresh/select/current/reset` 生命周期；
- AgentLoop、banner、session metadata 和 memory 辅助调用仍直接读取 `config.MODEL_ID`，即使写一个选择文件也不会形成真实闭环。

### 35.3 实现结果

模型发现调用链：

```text
nz-coder models refresh [--provider NAME]
  -> 读取该 provider 专属 base URL/API key
  -> 仅允许 HTTPS 或 loopback HTTP
  -> 禁用环境代理和 HTTP redirect
  -> GET provider models endpoint
       OpenAI-compatible / Responses: Authorization Bearer + /models
       Anthropic: x-api-key + anthropic-version + /v1/models
       Gemini: x-goog-api-key + /models
  -> 最多 20 页、10000 个模型、单响应/状态文件大小受限
  -> 规范化、去重、排序
  -> 原子写入 <workspace>/.nz-coder/models/catalog.json（0600）
```

选择调用链：

```text
nz-coder models select PROVIDER/MODEL [--variant NAME]
  -> 校验 provider、model identity 和 exact catalog variant
  -> 原子写 selection.json（不写 API key）
  -> 下次 AgentLoop 创建时读取 workspace selection
  -> 用所选 provider 创建 adapter
  -> 固定 model_id + immutable capability snapshot
  -> banner、session metadata、status 和 memory LLM 辅助调用使用同一 model_id
```

CLI 行为：

- `models refresh`：唯一会访问网络的模型命令；普通 Agent 启动不会自动联网发现；
- `models list [--provider] [--details]`：合并 discovery cache 与 A025 本地 exact catalog，离线显示 family、context/output、tools 能力；
- `models select`：保存 workspace 选择，可选择 A025 定义的 reasoning variant；
- `models current`：标明选择来自 `workspace` 还是默认 `configuration`；
- `models reset`：删除 workspace 选择，恢复环境配置。

### 35.4 关键设计决策

1. **发现必须显式触发**：普通启动若自动枚举，会增加延迟、把 API key 发送到额外 endpoint，并让离线使用不稳定。
2. **缓存绝不保存 credential**：只保存 provider、model ID、展示名、owner 和刷新时间；权限固定为 0600。
3. **禁用 proxy 与 redirect**：API key 只能发往用户配置的原 origin；redirect 不能把 Authorization 或 `x-api-key` 带往另一主机。
4. **HTTP 只允许 loopback**：本地兼容服务和测试可用明文 HTTP，远程发现必须 HTTPS。
5. **有界分页而不是只取第一页**：Anthropic 的 `has_more/last_id` 和 Gemini 的 `nextPageToken` 会继续获取，但页数和总模型数有硬上限。
6. **workspace 选择覆盖进程默认但不修改模块全局量**：沿用 A014 ContextVar workspace 隔离，不回退到修改 `config.MODEL_ID` 的旧做法。
7. **保留自定义模型能力规则**：Provider 的 `/models` 通常只返回 identity，精确能力仍由 A025 本地 catalog 优先覆盖，未知模型使用保守 family inference。

### 35.5 关键文件

- `nz_coder/providers/models.py`：三类协议发现、分页/安全边界、cache、selection 和能力投影；
- `nz_coder/providers/cli.py`：models 子命令与离线列表；
- `nz_coder/providers/capabilities.py`：允许 Agent 使用 workspace selection 的显式 variant；
- `nz_coder/runtime/loop.py`：选择 provider/model 并把同一 model ID 传给 memory 辅助调用；
- `nz_coder/interface/cli.py`：顶层命令路由和有效模型 banner；
- `nz_coder/state/sessions.py`、`nz_coder/state/workspace.py`：持久元数据与 status 使用有效模型；
- `tests/test_model_discovery.py`：真实 loopback 协议、安全、持久选择、CLI 和 AgentLoop 闭环测试。

### 35.6 验证结果

- `py_compile`：新增/修改 Provider、CLI 和 Loop 模块通过；
- Ruff：完整 `nz_coder/providers`、相关 runtime/interface 和定向测试 `All checks passed!`；
- 定向测试：9 项 discovery/cache/selection/CLI/Agent 测试通过；
- 聚焦回归：Provider、capability、CLI 与 smoke 共 `114 passed`；
- 完整回归：`763 passed, 3 warnings`；
- 真实冒烟：测试内启动独立 loopback HTTP Server，分别验证 OpenAI-compatible Bearer、Gemini header/filter、Anthropic 两页遍历和无 credential 落盘；
- 没有访问公网、没有读取真实 API key、没有发送计费推理请求、没有运行 SWE-bench。

### 35.7 学习重点

1. 模型目录、模型能力和当前模型是三个状态层；只有三者接入同一个 Session 创建点才算闭环。
2. `/models` 通常证明“模型存在”，不能证明 context window、工具调用或 reasoning 参数；能力元数据必须有独立可信来源或本地覆盖。
3. picker 写文件很简单，困难的是确保 provider adapter、prompt、token budget、memory、trace 和 session metadata 不再各自读取旧默认值。
4. discovery 是带 credential 的网络操作，redirect、proxy、明文传输和无界响应都属于安全边界。
5. 缓存让 list/picker 可离线、可复现；显式 refresh 则把网络副作用放到用户可见的命令上。

### 35.8 剩余差距

- InfCode 的 models.dev catalog 包含价格、模态、发布日期、attachment、完整 context/output 和大量 provider 元数据；NZ-Coder 当前 discovery 主要获得模型 identity，能力仍靠内置规则和 A025 exact catalog；
- 尚未实现 models.dev snapshot、5 分钟自动 freshness、跨进程 file lock 或离线内置大目录；
- OpenAI-compatible provider 差异很大，部分私有网关可能没有标准 `/models` 或返回非标准 schema；
- CLI 是非交互式可脚本化 picker，没有 TUI 搜索、最近使用、收藏或 GUI/IDE 控件；
- workspace selection 影响新建 Agent，运行中的 Session 不做热切换，以保持 capability snapshot 不变量；
- 没有对真实公网 Provider 做 live smoke；需用户明确授权并提供对应 credential 后才能验证第三方互操作。

## 36. A033：models.dev 精确能力目录

### 36.1 InfCode 参考能力

- `packages/opencode/src/provider/models.ts`：定义 Provider/Model schema，读取 `models.dev/api.json`，使用 5 分钟磁盘 freshness、内置 snapshot、跨进程 `Flock` 和后台刷新；
- `packages/opencode/src/provider/provider.ts` 的 `fromModelsDevModel`：把 context/output、tool call、reasoning、temperature、cost、modalities 等目录字段转成运行时模型能力；
- `packages/opencode/src/provider/model-cache.ts`：对动态 Provider 模型列表做 memory/file cache、TTL、in-flight refresh 去重与失败状态跟踪；
- `packages/opencode/src/cli/cmd/models.ts`：`models --refresh` 强制更新 models.dev cache，verbose 模式展示完整模型 metadata。

A033 对齐其中最影响 Agent 正确性的部分：精确窗口、输出上限、工具调用、reasoning 和 temperature 能力进入不可变 Session snapshot。

### 36.2 NZ-Coder 原有不足

A032 的 `/models` discovery 能确认账号可见的模型 identity，但主流 Provider 通常不会在该接口返回：

- context window 和最大输出；
- 是否支持 tool call；
- 是否是 reasoning model；
- 是否接受 temperature；
- 模型 family、发布日期和其他产品 metadata。

因此未知模型仍会落到 family 字符串启发式或 100K/8K 保守默认。实际后果可能是提前压缩、输出预算过小、向不支持工具的模型发送 tools，或者向 reasoning 模型发送不兼容参数。

### 36.3 实现结果

Registry 同步链路：

```text
nz-coder models sync [--url URL] [--force]
  -> URL 仅允许 HTTPS / loopback HTTP
  -> 拒绝 URL credential、query、fragment
  -> workspace registry.lock + flock
  -> 锁内再次检查 source + mtime freshness（默认 300 秒）
  -> 无代理、无 redirect、10 MB 有界 GET
  -> 校验最多 500 providers / 50000 models
  -> 只保留 NZ-Coder 已有 adapter 可消费的 provider
  -> 规范化精确 capability record
  -> 0600 临时文件 + fsync + os.replace
  -> 非法新数据不覆盖上一个健康 snapshot
```

Capability 优先级现在固定为：

```text
builtin family inference
  < models.dev-compatible registry exact record
  < MODEL_CATALOG_JSON / MODEL_CATALOG_PATH exact record
  < MAX_CONTEXT_TOKENS / MAX_OUTPUT_TOKENS environment override
  < MODEL_CAPABILITIES_JSON active override
  < selected reasoning variant request options
```

例如 registry 声明 `context=222000, output=44000, tools=false, reasoning=true`，本地 catalog 只声明 `context=333000, tools=true`，最终能力会是：本地 context/tools 覆盖，registry 的 output/reasoning 继续保留。这样项目配置不必复制整条远端记录。

新增 CLI：

- `nz-coder models sync`：fresh cache 命中时不联网；`--force` 强制刷新；
- `nz-coder models registry-status`：显示 source、fetched time、provider/model 数和 fresh 状态；
- `nz-coder models list --details`：将 registry identity 与 A032 Provider discovery、本地 exact catalog 合并，并通过最终 capability policy 展示有效窗口和工具能力。

### 36.4 关键设计决策

1. **只显式 sync，不做后台定时联网**：InfCode 是完整 App/server，后台更新模型目录合理；NZ-Coder 当前仍以 CLI Agent 为主，启动时不应产生不可见网络副作用。
2. **先规范化再落盘**：不把任意远端 JSON 当成本地请求参数；只允许 family、context/output 和三个 boolean capability 字段进入 runtime。
3. **本地 exact catalog 优先**：私有 gateway 的模型名和行为可能与公共 registry 不同，workspace 操作者必须能修正公共数据。
4. **字段级 overlay，不是整条替换**：本地只声明需要修正的字段，未声明能力继续继承 registry。
5. **unsupported provider 不进入 cache**：没有 adapter 的模型即使能展示也不能运行，避免 picker 提供必然失败的选项。
6. **健康旧快照优先于坏刷新**：下载、schema 或限制检查完成后才原子替换；刷新失败不破坏离线启动。
7. **source URL 不允许 query credential**：source 会进入状态文件和 status 输出，因此不允许 token 以 URL query 或 userinfo 形式出现。
8. **workspace cache 而非全局 cache**：与 A014 workspace isolation 一致，不同项目可固定不同 registry source；代价是相同目录可能重复存储。

### 36.5 关键文件

- `nz_coder/providers/registry.py`：registry 下载、安全限制、schema 归一化、freshness、锁、原子 snapshot 和 capability projection；
- `nz_coder/providers/capabilities.py`：registry exact record 与本地 catalog/环境 override 的优先级合并；
- `nz_coder/providers/cli.py`：sync、registry-status 和 registry/offline list 合并；
- `nz_coder/config.py`、`.env.example`：source、workspace path 和 TTL 配置；
- `tests/test_model_registry.py`：真实 HTTP、精确能力、覆盖优先级、fresh/force、并发 single-flight、失败保活、安全 URL/path 和 CLI 闭环。

### 36.6 验证结果

- `py_compile`：registry、capabilities 和 CLI 通过；
- Ruff：完整 `nz_coder/providers`、CLI 和新增测试 `All checks passed!`；
- 新增定向测试：`10 passed`；
- Provider/Capability/CLI 聚焦回归：`124 passed`；
- 完整回归：`773 passed, 3 warnings`；
- 真实本地冒烟：独立 loopback HTTP registry，验证下载、header-free public request、0600 snapshot、fresh cache、force、四线程 single-flight、非法刷新保留旧 snapshot 和离线 list；
- 没有访问 models.dev 公网，没有读取真实 credential，没有发送模型推理请求，没有运行 SWE-bench。

### 36.7 学习重点

1. Provider `/models` 和 models.dev 解决不同问题：前者回答“账号看得到什么”，后者回答“这个模型能做什么”。
2. 公共能力目录不能直接成为请求 body；必须经过小型白名单 schema，才能避免远端 metadata 获得修改 header/tools/model 的能力。
3. overlay 的优先级应从公共默认走向本地事实，私有 gateway 的显式配置必须压过公共 registry。
4. cache 正确性包含 freshness、并发刷新、原子替换和失败保活，不只是把 HTTP 响应写进 JSON。
5. capability 在 AgentLoop 创建时冻结，registry 后续刷新不会改变正在运行的 Session，避免同一会话中预算和请求语义漂移。

### 36.8 剩余差距

- 尚未保存和展示 InfCode registry 中的 cost、cache pricing、modalities、attachment、knowledge、status、provider npm/api 和 experimental modes；
- 没有内置 models snapshot，首次离线且 workspace 无 cache 时仍回退 A015 family inference；
- 没有 InfCode 的后台启动刷新和每小时 refresh schedule；当前只有显式 CLI sync；
- 没有 HTTP ETag/If-Modified-Since，freshness 到期后会重新下载完整目录；
- Windows 无 `fcntl` 时仍保留原子写，但当前 stdlib fallback 不提供跨进程 flock；
- generic `openai-compatible` 无法自动判断背后实际 vendor；要使用 registry 精确记录，应选择 `openai`、`deepseek`、`dashscope` 等命名 provider；
- 未对 models.dev 公网做 live smoke；用户明确授权联网后可执行一次 `nz-coder models sync --force` 验证真实 schema 互操作。

## 37. A034：Dodo 平行架构物理收敛

### 37.1 InfCode 参考能力

InfCode 的 `ARCHITECTURE.md` 与当前源码把产品边界分为 core server、Session、SDK/bridge、host/client 和 indexing 等明确层次；客户端消费统一 Session 服务，而不是在同一仓库里并列维护两套任务控制面、事件协议和 Agent worker。

A034 不复制某个 InfCode 文件，而是对齐这种架构收敛原则：同一职责只有一个主实现，产品外壳必须消费 core contract，不能形成独立演化的第二套后端。

### 37.2 NZ-Coder 原有不足

历史 Dodo 代码来自外部架构，后来与 NZ-Coder 主干并存：

- `dodo_server_min.py` 自带 FastAPI 多租户 API、鉴权、SSE、scheduler 和中心 memory；
- `dodo/task_runner.py`、`launcher.py`、`control_plane.py` 自带 subprocess/Docker task lifecycle；
- `dodo/headless.py`、`dev_headless.py` 自带 Agent worker 和开发任务流程；
- `dodo/dodo_trace.py` 自带外部 data-report 镜像；
- `dodo/memory_*` 自带另一套 store/sync/embedding backend；
- `pyside_client/` 只消费 Dodo hosted API，不消费 A018–A023 的 NZ-Coder Session API；
- 10 个专属测试文件、Docker vertical slice、安装脚本和两套集成文档持续让它看起来像仍受支持的正式产品。

只做“默认不导入 Dodo”仍然不够：面试时无法清楚解释究竟哪个 server、事件协议、任务生命周期和客户端才是项目主架构。

### 37.3 审计方法与结果

调用图按三层检查：

```text
安装入口 / pyproject / nz-coder CLI
  -> 无 Dodo / PySide 命令

core production package
  -> AgentLoop / HTTP / MCP / Session / memory / trace 无 Dodo import

剩余引用
  -> Dodo 模块内部互相引用
  -> dodo_server_min / demo / build script
  -> Dodo/PySide 专属 tests 和历史 docs
```

结论：生产 caller graph 为空。原 44 项 Dodo/PySide 测试只验证该平行产品自身，不能证明 NZ-Coder core 需要兼容入口。

已内化而保留的能力映射：

| Dodo 旧职责 | NZ-Coder 唯一主实现 |
|---|---|
| headless Agent worker | `runtime/loop.py` + `http_service/` |
| 多 workspace/session routing | `http_service/manager.py` 与持久 Session API |
| SSE/task event | `session_events.py` + HTTP SSE journal/replay |
| 并行隔离任务 | `runtime/agent_manager.py` |
| trace/data report | 本地 `state/trace.py` + Session events；不隐式外传 |
| memory store 注入 | `state/memory.py` 的 project-owned contract |
| desktop/remote client contract | 标准库 `http_service/client.py` 消费统一 Session API |

物理删除范围共 39 个文本源码/测试/文档文件：

- `nz_coder/dodo/` 15 个 Python 源文件；
- `nz_coder/pyside_client/` 4 个 Python 源文件；
- 10 个 Dodo/PySide 专属测试文件；
- `dodo_server_min.py`、vertical-slice demo/example、旧 INSTALL/build/requirements 和两份 Dodo 集成文档；
- 旧 `requirements-client.txt` 与含已失效 Dodo tracer 的 `runtime/loop.py.orig`。

### 37.4 关键设计决策

1. **不保留抛异常的兼容空壳**：没有生产 caller 时，空包只会继续暗示支持承诺；导入失败比运行到一半才报“已废弃”更明确。
2. **不把整个 Dodo 控制面搬进 core**：A017–A023、A030 已提供项目自有 Session/Event/HTTP/background-agent 能力；再搬一次只会保留重复实现。
3. **不迁移 PySide 客户端**：它绑定旧 Dodo endpoint/schema。没有明确 App consumer 前，不为展示而维护第二个 GUI；未来客户端应使用统一 Session API。
4. **删除专属测试而非改名保留**：被删除产品的自测不属于 core 回归。用新的架构边界测试防止旧源码悄悄回流。
5. **不删除运行数据**：`.dodo-server/memory.db` 可能包含用户历史数据，不属于源码清理授权；本次明确保留。
6. **不依赖 Git 才能做架构判断**：调用图、入口和测试消费者提供证据；Git 只影响恢复方式，不决定代码是否属于主架构。

### 37.5 关键文件

- `docs/architecture.md`：从“隔离但仍保留”更新为已删除后的唯一主架构和能力映射；
- `docs/nzcoder_core_architecture.md`：移除“另有 Dodo/PySide 分支”的范围声明；
- `tests/test_architecture_boundary.py`：禁止 Dodo server、Dodo Python source、PySide source 和专属 requirements 回流；
- 被删除的 `nz_coder/dodo/`、`nz_coder/pyside_client/`、server/demo/test/install 文档不再构成维护面。

### 37.6 验证结果

- 删除前基线：10 个 Dodo/PySide 测试文件共 `44 passed, 2 warnings`；
- 调用图复查：除新的防回归测试和历史学习日志外，活跃源码/配置不再包含 `nz_coder.dodo`、`dodo_server_min`、`pyside_client` 或 `DODO_*` 引用；
- 新增架构边界测试：`1 passed`；
- HTTP/Memory/Event/Agent Manager 核心替代链路聚焦回归：`75 passed`；
- 完整回归：删除 44 项旧产品测试、新增 1 项架构测试后为 `730 passed, 1 warning`；测试总数下降来自产品范围删除，不是回归丢失；
- 没有运行 SWE-bench，没有访问公网，没有删除 `.dodo-server/memory.db`。

### 37.7 学习重点

1. “代码默认不调用”不等于架构已经收敛；只要第二套 server/schema/tests/docs 仍存在，它就仍然制造维护承诺。
2. 内化应按能力和 contract 进行，不是把借来的目录整体搬进 core。
3. 删除测试有时是正确行为：测试数量不是目标，被删除产品的测试继续存在反而会阻止真正收敛。
4. 架构清理必须先证明 caller graph 为空，再删除；目录名或个人印象不足以作为证据。
5. 源码与运行数据的授权边界不同。即使旧产品被删除，也不能顺手删除可能含用户内容的数据库。

### 37.8 恢复与剩余差距

- 这批 Dodo/PySide 源文件此前未被当前 Git index 跟踪，不能通过 `git restore` 恢复；如需恢复，应从最初取得 Dodo 架构的来源重新导入，而不是从 core 反向拼装；
- `.dodo-server/memory.db` 已保留，可由用户自行归档或迁移；NZ-Coder core 不会再读取它；
- Python `__pycache__` 可能暂时保留已删除模块的字节码，但没有 `.py`/包入口，不属于可维护源码，后续可作为生成物清理；
- 统一 Session API 目前只有标准库薄客户端，没有正式 GUI、VS Code 或 JetBrains consumer；应由真实产品需求驱动，而不是复活旧 PySide schema；
- `http_service` 是本地 loopback Session service，不等同 InfCode 完整 SDK/IDE 生态。

## 38. A035：统一扩展描述与状态 Contract

### 38.1 InfCode 参考能力

- `packages/opencode/src/plugin/index.ts`：Instance-owned plugin state、internal/external plugin 初始化、统一 hook list/trigger 和 Bus event subscription；
- `packages/opencode/src/plugin/loader.ts`：插件 specifier、加载来源、兼容性/安装/entry 错误分阶段报告；
- `packages/plugin/src/index.ts`：统一 Plugin input、Hooks、tool/auth/config/event 等类型 contract；
- `packages/opencode/src/tool/registry.ts`：把 built-in、plugin、dynamic/MCP tools 合并为 Session 可用工具集合。

InfCode 已经有真实 npm/plugin consumer。NZ-Coder 当前没有第三方插件包，因此 A035 只对齐最上游、不会制造执行风险的部分：统一描述、状态、来源故障隔离和可观察性。

### 38.2 NZ-Coder 原有不足

NZ-Coder 原有四类扩展能力都可用，但没有共同语言：

| 扩展面 | 原有身份/状态 | 原有生命周期 |
|---|---|---|
| Skills | name/source/allowed_tools/paths | project→user→bundled、lazy body、reload |
| Hooks | Python callback 或 configured id/event/action | Agent 创建时构建，configured hook 可 reload |
| Optional tool packs | name/module/tool_names/loaded | lazy import，只能进程内加载 |
| MCP | server config/status 与动态 tool binding | Session-owned connect/reconcile/disconnect |

因此无法回答“当前 workspace 有哪些扩展、来自哪里、是否可信、会产生什么 effect、坏了哪一类来源”，也无法为未来插件 consumer 复用一个稳定 contract。

### 38.3 实现结果

统一 descriptor：

```text
ExtensionDescriptor (contract_version=1)
  identity: extension_id / kind / name
  ownership: source / scope
  safety: trusted / effects / permissions
  runtime: status / lifecycle / error
  surface: description / capabilities
```

稳定 ID 使用 namespaced 形式：

```text
skill:code-review
hook:core
hook:<configured-id>
tool_pack:lsp
mcp_server:<server-name>
error:<source>
```

四类投影：

- SkillLoader 新增 `list_skills()`，只返回 frontmatter metadata，不加载 body；project/user/bundled precedence 和 conditional 状态保持不变；
- core hooks 聚合为 `hook:core`，configured hooks 单独显示 event/action/reject/continue/on_error；
- optional pack manifest 在模块未 import 前就声明每个工具的 `read/serial/write` effect，加载后再用真实注册 effect 校准；
- MCPRuntime 新增 secret-free `extension_snapshot()`，只公开 server source/trust/status/transport、tool name/effect、prompt/resource count 和错误类型，不公开 command、environment、header、token 或 URL credential。

来源隔离：

```text
collect skills
collect hooks
collect tool packs
collect MCP
  -> 每个 collector 独立 try/validate
  -> 一个来源失败：生成 error:<source>
  -> 其他 descriptor 继续返回
```

CLI：

```bash
nz-coder extensions list
nz-coder extensions list --kind mcp_server
nz-coder extensions list --json
nz-coder extensions status tool_pack:lsp
nz-coder extensions status skill:code-review --json
```

CLI 只检查 metadata：不会加载 optional Python module、启动 MCP command、连接远程 server 或读取 Skill body。

### 38.4 关键设计决策

1. **统一 contract，不统一执行机制**：Skill prompt、Hook decision、tool import 和 MCP process 的生命周期本来就不同；强迫共享一个 loader 会破坏各自安全边界。
2. **只读 projection 优先**：没有真实第三方插件 consumer 前，不增加自动安装和任意 Python import；先让现有扩展面可审计。
3. **immutable snapshot**：descriptor 是一次观察结果，不是可变全局 registry；MCP live reconcile、Skill reload 后重新 snapshot 即可得到新 generation。
4. **namespaced ID 防碰撞**：同名 skill、hook、pack、MCP server 可以共存，不需要全局抢占一个字符串命名空间。
5. **effect 必须在 lazy load 前声明**：否则未加载的结构编辑工具会被误报成 `serial`，权限审计要等代码执行后才准确。
6. **来源失败不清空全局列表**：项目 hooks JSON 错误不应让 bundled skills、packs 和 MCP 状态一起消失。
7. **trust 是观察事实，不是新的授权旁路**：MCP project command 的 trust 仍由 A031 强制执行；project Skill/Hook 显示为 untrusted workspace content，但 A035 不偷偷改变它们的既有加载行为。
8. **不暴露路径和 credential-bearing config**：统一状态面不是调试 dump；CLI 不输出 MCP command/env/header，也不读取 Skill body。

### 38.5 关键文件

- `nz_coder/extensions/registry.py`：descriptor schema、四类 collector、scope/source 映射和 source error isolation；
- `nz_coder/extensions/cli.py`：list/status、kind filter、JSON 和文本 projection；
- `nz_coder/state/skills.py`：lazy、secret-free skill metadata enumeration；
- `nz_coder/runtime/hooks.py`：向 extension registry 提供 strict settings validation，同时保持 Agent 默认容错行为兼容；
- `nz_coder/tools/__init__.py`、`tools/optional_loader.py`：optional pack pre-load effect manifest；
- `nz_coder/mcp/runtime.py`：live MCP extension snapshot；
- `nz_coder/interface/cli.py`：顶层 extensions 路由；
- `tests/test_extensions.py`：contract、四类投影、信任、effect、故障隔离和 CLI。

### 38.6 验证结果

- `py_compile`：extensions registry/CLI、Skill 与 MCP 修改通过；
- Ruff：extensions、Skill、Hook、optional packs、MCP、CLI 和新增测试 `All checks passed!`；
- 新增定向测试：`10 passed`；
- Extension/Hook/MCP/Smoke 聚焦回归：`100 passed`；
- 完整回归：`740 passed, 1 warning`；
- 本地真实 CLI：成功列出 `hook:core`、`skill:code-review`、`tool_pack:lsp`、`tool_pack:python_ast`，并确认未加载 LSP 的 effect 仍精确为 `read`；
- 没有导入 optional pack、没有启动 MCP server、没有访问公网、没有运行 SWE-bench。

### 38.7 学习重点

1. 扩展平台首先是 ownership 和 safety contract，其次才是安装器。
2. 统一描述不等于统一实现；不同生命周期可以投影到同一 schema，同时仍由各自 owner 执行。
3. lazy extension 的 manifest 必须在执行前足够完整，否则权限系统无法提前判断风险。
4. 状态 API 应默认 secret-free。调试便利不能成为输出 command、env、header 或 token 的理由。
5. 一个坏插件不应让整个扩展系统不可见；source-level failure descriptor 比静默忽略更适合诊断。
6. `trusted` 字段只有在明确区分“观察”和“强制执行”时才有意义；A035 没有伪称 project Skill/Hook 已有审批门。

### 38.8 剩余差距

- 没有 InfCode npm plugin loader、安装、版本解析、兼容性协商、server/client 双 entry 或 Plugin SDK；
- 没有统一 extension 执行/transition API；load/reload/connect/disconnect 仍由 SkillLoader、tool pack loader 和 MCPRuntime owner 负责；
- project Skill/Hook 的 untrusted 标记目前是可观察 metadata，不是独立用户审批门；MCP project command trust 才是强制执行；
- configured hooks 仍只有 schema-limited prompt/reject/continue action，没有任意代码 hook；这是当前安全边界；
- optional Python packs 仍是 bundled modules，不支持第三方 package discovery；
- CLI snapshot 是当前进程/配置视图，不能附着到另一个正在运行的 HTTP Agent Session；
- 尚无真实第三方 extension consumer，因此不应继续实现 marketplace、自动更新、auth hook 或任意代码加载。

## 39. A036：当前差距再审计与 release baseline

> 本节是 A036 时点快照，已由 A046 的实机故障驱动审计更新。尤其是
> terminal、HTTP、Provider、代码理解和 extensions 的“已对齐/冻结”表述，
> 应以 A046 的分级矩阵为准。

### 39.1 审计目标与参考

本项不增加 runtime 功能，而是重新读取 A029–A035 后的 NZ-Coder 实现、当前本地 InfCode 架构快照和面向用户的文档，回答三个问题：哪些核心能力已经闭环、哪些只缺真实互操作或评测证据、哪些属于没有 consumer 的产品生态。

参考重点包括 InfCode 的 Session/server、Provider/model registry、MCP、Agent Manager、plugin registry 与 SDK/IDE 边界，以及 NZ-Coder 对应的 `http_service/`、`providers/`、`mcp/`、`runtime/agent_manager.py`、`extensions/` 和持久代码索引。没有访问公网，也没有把目录数量当成功能完成度。

### 39.2 被纠正的旧判断

第 30 节截止 A028，随后已经发生六项会改变结论的实现：A029 持久增量代码索引、A030 后台写 Agent、A031 MCP 分层配置与 live reconcile、A032/A033 模型发现和能力目录、A034 删除 Dodo/PySide 平行产品、A035 统一扩展描述。因此第 30 节被保留为学习历史并明确标记“已取代”，避免文档同时声称能力缺失和能力已完成。

### 39.3 当前能力矩阵

| 维度 | 当前结论 | 仍未覆盖的边界 |
|---|---|---|
| runtime/workspace 隔离 | **核心已对齐，默认冻结** | 远程多租户资源治理不属于本地终端产品边界。 |
| context/compaction | **核心已对齐，默认冻结** | 可继续用真实 provider usage 校准，但不是结构缺口。 |
| 持久索引/LSP | **终端 Agent 核心已对齐** | 非 Python 精确静态引用仍依赖已安装 LSP；Tree-sitter/vector 仅在真实检索需求出现时进入。 |
| Provider/model | **协议、能力快照、发现与选择基础已对齐** | 公网服务的 live interoperability 仍是证据缺口；不承诺每个 vendor 都有原生 SDK。 |
| MCP | **核心协议与生命周期已对齐，默认冻结** | 任意第三方公网 server 的兼容矩阵尚未验证；HTTP 管理 API 需真实客户端 consumer。 |
| 后台写 Agent | **安全编排主链路已对齐** | IDE task card、云任务队列和跨主机调度属于宿主产品能力。 |
| Session HTTP | **本地闭环已对齐，默认冻结** | 尚无官方 GUI、VS Code、JetBrains 或生成式 SDK consumer；当前服务不是远程多用户平台。 |
| extensions | **现有四类扩展的 metadata contract 已对齐** | 第三方 package runtime、marketplace、任意代码 hook 必须由真实外部插件驱动。 |
| Dodo/PySide | **已解决** | 平行产品源代码已删除；不再作为待办或兼容目标。 |
| SWE-bench | **证据未收口，deferred P0** | 缺固定 commit/model/config 的完整 300 Lite 官方结果和可复现归档；遵从用户要求本项不运行。 |

### 39.4 Release 分类

- **frozen core**：HTTP Session 正确性、MCP 核心、运行时隔离、上下文、持久代码索引、后台写 Agent、Provider discovery foundation 和扩展 metadata；只接受真实 bug、互操作问题或 consumer 驱动的变更。
- **deferred evidence**：SWE-bench 官方 300 Lite，恢复评测时立即成为最高优先级证据工作。
- **interoperability validation**：真实 Provider 和公网 MCP 的 live smoke，需要凭据、网络目标和明确授权，不能用本地 fixture 冒充。
- **consumer-driven product work**：插件安装/marketplace、SDK、GUI/IDE host、remote control plane、Tree-sitter/vector 多后端；没有命名 consumer 和验收测试时不继续横向复制 InfCode。

### 39.5 关键文件

- `docs/release-baseline.md`：当前唯一简明的支持范围、冻结边界、限制和 release claim checklist；
- `README.md`：同步当前架构、model/MCP/extensions 入口和限制；
- `docs/architecture.md`：纠正旧 MCP transport/OAuth 描述并确认 Dodo 已移除；
- `docs/infcode-alignment-learning-log.md`：保留历史决策，同时指向当前 baseline；
- `tests/test_release_docs.py`：阻止关键用户文档重新出现已经失效的断言。

### 39.6 验证结果

- 文档 reader checks：`5 passed`，覆盖产品边界、入口/网络动作区分、过时断言、历史矩阵指向和相对链接；
- CLI 离线冒烟：`python -m nz_coder --help`、`models current`、`extensions list`、`mcp list` 均成功；
- 完整回归：`745 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 没有访问模型、Provider registry 公网 endpoint 或 MCP server；
- 没有运行 SWE-bench。

### 39.7 学习重点

1. 对齐不是无限复制目录，而是让已选择的产品边界形成可验证闭环。
2. 历史差距表必须标注观察时点；否则完成越多，文档反而越容易误导读者。
3. “本地 fixture 通过”“公网互操作通过”“正式评测结果”是三种不同证据，不能混写。
4. 冻结不是永不修改，而是要求 bug、consumer 或互操作证据作为重新进入条件。
5. release baseline 应让第一次接触项目的人在一页内看清支持范围、不支持范围和下一步进入条件。

### 39.8 剩余差距

- deferred P0：完整、固定口径、可复现的 SWE-bench Lite 官方证据；
- interoperability：经授权的真实 Provider/MCP smoke 与兼容性记录；
- consumer-driven：第三方插件 runtime、正式 SDK/GUI/IDE host 和 remote platform；
- code intelligence 广度：非 Python 静态精确引用、Tree-sitter/vector 只在真实任务证明现有索引不足时继续。

## 40. A037：prompt_toolkit 终端交互基础

### 40.1 InfCode 参考能力

- `packages/opencode/src/cli/cmd/tui/app.tsx`：Solid/OpenTUI 全屏应用、route/context/provider、command dialog、model/session/theme/MCP dialog；
- `component/prompt/index.tsx` 与 `autocomplete.tsx`：多行 textarea、历史、stash、slash command、文件/Agent mention 和状态 footer；
- `routes/session/permission.tsx`、`question.tsx`、`sidebar.tsx`：结构化交互卡片和持续状态区域。

InfCode 的终端不是若干 `print()`，而是 Session HTTP/SSE 的完整视觉客户端。NZ-Coder 当前不需要复制 TypeScript/Solid 技术栈，但日常使用的输入闭环不能继续停留在单行 `console.input()`。

### 40.2 NZ-Coder 原有不足

- `prompt_toolkit` 已是正式依赖，却没有被主 REPL 使用；
- 输入只有单行读取加 30ms 粘贴 drain，没有光标友好的多行编辑、持久历史或补全菜单；
- `/help` 只能事后查看静态列表，文件引用和 Session ID 需要完整手输；
- 模型只能退出 Agent 后运行顶层 `nz-coder models select`，当前 Agent capability snapshot 不会随文件改变；
- banner 只在启动时显示，运行中看不到模型、mode、Session 和粗略 context 状态。

### 40.3 实现结果

主链路变为：

```text
async Agent CLI
  -> TerminalInput.read_async()
  -> one PromptSession
       Enter submit / Alt+Enter newline
       FileHistory + AutoSuggestFromHistory
       TerminalCompleter: /command, /resume, /mode, /model, @workspace-file
       dynamic toolbar from current session_state Agent
  -> existing slash dispatch or AgentLoop.run()
  -> invalidate file completion snapshot after the run
```

具体行为：

- 交互 TTY 使用异步 `PromptSession.prompt_async()`，不会在 Agent 的 asyncio loop 中嵌套 `asyncio.run()`；
- 非 TTY、pipe 和测试环境继续走原 `console.input()`/多行粘贴回退；
- history 位于 workspace `.nz-coder/prompt-history`，目录尽力设为 `0700`、文件为 `0600`；
- `@` 文件候选只来自 workspace 内普通文件，不跟随 symlink，忽略 `.git`、`.nz-coder`、`node_modules`、venv/build 等目录，最多扫描 10000 项；
- toolbar 动态投影当前 Agent provider/model、permission mode、Session ID 和 context 估算，不复制运行时状态；
- 新增 `/keys`；新增 `/model [list|reset|PROVIDER/MODEL [VARIANT]]`，模型选择后在同 Session 重建 Agent并关闭旧 owner；创建失败会恢复原 workspace selection；
- model/file completion 只读本地 cache/registry/workspace，不自动访问网络。

### 40.4 关键设计决策

1. **先做增强型 scrolling REPL，不复制 OpenTUI**：`prompt_toolkit` 已在依赖中，足以解决最影响使用的编辑、历史、补全和状态问题，且不引入新的 UI 框架。
2. **输入层不拥有业务状态**：toolbar 每次从 `session_state["agent"]` 取 snapshot；Session/model 的唯一 owner 仍是现有模块。
3. **同步与异步入口分开**：真实 PTY 首次冒烟发现同步 `prompt()` 会在现有 asyncio loop 中嵌套事件循环，因此主链路固定使用 `prompt_async()`。
4. **补全必须有路径边界**：不递归 symlink、不读取 workspace 外文件，不把运行历史、Git objects 或依赖目录塞进候选。
5. **模型切换是 owner replacement**：只写 selection 文件不足以改变已创建 Agent；必须在同一 Session 创建新 Agent、成功后替换并关闭旧 Agent，失败则回滚 selection。

### 40.5 关键文件

- `nz_coder/interface/terminal_input.py`：PromptSession owner、completion、history、toolbar、路径扫描和非 TTY fallback；
- `nz_coder/interface/cli.py`：async 输入接线、动态状态 snapshot 和运行后文件候选刷新；
- `nz_coder/interface/commands/handlers/core.py`：`/keys` 与同 Session `/model` 生命周期；
- `tests/test_terminal_input.py`：输入层、completion、路径与权限测试；
- `tests/test_cli_commands.py`：模型替换和失败回滚测试。

### 40.6 验证结果

- Ruff：A037 源码与测试 `All checks passed!`；
- CLI/terminal/smoke/release-doc 聚焦测试：`69 passed`；
- 真实 PTY：成功启动、执行 `/keys`、回到提示符并 Ctrl+D 正常退出；首次冒烟捕获并修复同步 prompt 嵌套 event loop 问题；
- 完整回归：`756 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 没有发送模型请求、没有访问公网、没有启动 MCP server、没有运行 SWE-bench。

### 40.7 学习重点

1. 后端 Session 能力完整不代表终端 consumer 好用；CLI/TUI 必须作为单独产品层审计。
2. 交互库的同步 API 在普通单测中可能正常，却会在真实 asyncio PTY 中失败，真实终端冒烟不可省略。
3. 模型 picker 的完成条件不是“写入 selection”，而是当前 Agent capability/client 真正切换且旧 owner 被释放。
4. 自动补全也是安全边界：候选生成不能无界扫描、跟随外部 symlink 或泄露内部 runtime artifacts。

### 40.8 剩余差距

- 仍是 scrolling REPL，不是 InfCode 全屏 TUI；没有 sidebar、timeline、session rename/delete/fork dialog、theme/mouse；
- 工具调用仍是名称加 500 字符预览，缺按 read/write/bash/task 分类的可折叠状态卡和 diff viewer；
- Permission/Question 虽可交互，但还是线性文本，不是统一视觉卡片；
- `@file` 目前是文本引用补全，不生成 InfCode FilePart，也没有图片/剪贴板附件；
- completion menu 有模型和 Session 候选，但还没有独立 fuzzy dialog；
- context 数值是本地估算，不是 provider usage/cost 实时累计。

## 41. A038：Session-event 驱动的结构化终端运行视图

### 41.1 InfCode 参考能力

- `routes/session/index.tsx`：按 Session event/state 渲染消息、工具、permission、question 与 run 状态；
- `routes/session/permission.tsx`、`question.tsx`：交互请求不是裸 `input()` 文本，而是带状态和选项的视觉区域；
- `routes/session/sidebar.tsx`、`footer.tsx`：持续展示任务和 pending 状态；
- tool part renderer：按工具种类显示摘要、结果与错误，而不是无差别打印原始输出。

### 41.2 NZ-Coder 原有不足

A037 解决了“怎么输入”，但运行展示仍由 `on_tool(name, output)` 直接打印：

- 所有工具统一显示名称和前 500 字符，无法区分 read/edit/command/agent/state；
- 没有 duration、command nonzero、run settlement 或 changed-file 汇总；
- 外部命令输出中的 ANSI/control sequence 未显式清理；
- SessionEventBus 已包含 run/tool completed 事实，但终端没有消费，形成 HTTP consumer 比本地 CLI 更结构化的倒挂；
- 未预期异常会退出整个 REPL，用户不能修正配置后继续；
- Permission 和 Question 仍是逐行文本。

### 41.3 实现结果

事件链路：

```text
ToolExecutor.execute_one
  -> session.tool.started (id/index/name/category/summary/is_write)
  -> permission + dispatch
AgentLoop._trace_tool_result
  -> session.tool.completed
     (status/duration/category/summary/output_len/bounded-or-persisted output)
  -> legacy on_tool callback
TerminalRunRenderer.on_tool
  -> drain the authoritative events
  -> one compact Rich card

session.run.started/completed/failed/cancelled
  -> terminal settlement line + elapsed + tool count + changed paths
```

终端行为：

- tool card 显示 read/edit/command/agent/state、名称、参数摘要、状态和时长；
- 成功结果显示有限前部，command/nonzero/error 优先显示尾部，隐藏行数可见；完整结果仍在消息/trace 或 persisted-output 文件中；
- ANSI escape 和不可显示 control character 在渲染前删除，避免工具输出控制终端；
- `ChangeTracker.current_changed_paths()` 只作为 run-end projection，显示变更文件数和最多五个路径；
- Permission/Question 在真实 Rich console 使用黄/青卡片，fake/simple console 保留文本兼容；
- 意外异常消费 `session.run.failed` 后回到 REPL，不再直接终止 CLI；Ctrl-C 显示 cancelled settlement；
- EventBus 不可用的测试/兼容 Agent 才使用 callback fallback，且通过 call ID 去重。

### 41.4 关键设计决策

1. **Session events 是唯一事实源**：CLI 不从 trace 反解析工具状态，也不建立第二份 mutable run store。
2. **先 publish completion，再触发 legacy callback**：callback 只充当同步 drain point，终端拿到的是带分类和时长的完整 event；HTTP/SSE 同时受益于 schema enrichment。
3. **started 在 ToolExecutor 权限检查前发布**：客户端能知道 Agent 正准备执行什么；ContextVar EventBus 在线程并发调度中仍保持 Session 隔离。
4. **终端 preview 必须有安全和噪声上限**：完整 stdout 不适合每次铺满滚动区，控制字符也不能被当作可信 UI 指令。
5. **不做后台 UI consumer thread**：当前 callback drain 避免并发 Rich render 与 permission input 抢终端；若未来做全屏 TUI，再由单一 UI event loop 消费。

### 41.5 关键文件

- `nz_coder/interface/run_renderer.py`：event subscription、tool/run card、预览策略、控制字符清理、changed paths 与 interaction cards；
- `nz_coder/runtime/tool_executor.py`：permission/dispatch 前的 `session.tool.started`；
- `nz_coder/runtime/loop.py`：completed event schema enrichment 和 callback 顺序；
- `nz_coder/interface/cli.py`：per-run subscribe/drain/settle/close 与异常后继续；
- `nz_coder/tool_platform/permissioning/interaction.py`、`interface/questions.py`：Rich card + simple-console fallback；
- `tests/test_run_renderer.py`：去重、fallback、sanitization、changed paths、interaction cards 和 started event context。

### 41.6 验证结果

- Ruff：A038 源码与测试 `All checks passed!`；
- run-renderer/CLI/permission/question/event/smoke 聚焦测试：`102 passed`；
- 离线真实终端卡片冒烟：成功显示 Working、Read tool card（summary/duration/output）和 Run completed；
- 完整回归：`761 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 没有发送模型请求、没有访问公网、没有启动 MCP server、没有运行 SWE-bench。

### 41.7 学习重点

1. callback 适合兼容，event 才适合多个客户端共享同一状态语义。
2. 结构化展示的价值不只是美观：它区分 dispatch error、command nonzero 和正常结果，减少用户误判。
3. 外部工具输出属于不可信展示输入；路径边界之外，还必须考虑终端控制序列注入。
4. 真正的 run 完成提示必须来自 settlement event，而不是“模型停止输出 token”这一视觉猜测。
5. scrolling REPL 可以先获得高价值的卡片和状态语义，不必为了对齐体验立即复制完整前端框架。

### 41.8 剩余差距

- 卡片仍是不可折叠的滚动输出，没有 InfCode timeline/sidebar 和消息定位；
- started event 当前在 callback drain 时随 completed 一起呈现，没有持续 spinner/并行 tool progress；
- 没有 session rename/delete/fork、theme、mouse、clipboard/image attachment dialog；
- Permission/Question 是 Rich 卡片加文本输入，还不是方向键选择控件；
- changed-file 只有路径摘要，详细 diff 仍通过 `/diff` 查看；
- usage/cost 仍是本地 token 估算，未投影 provider usage。

## 42. A039：CLI Session timeline 与安全会话 fork

### 42.1 InfCode 参考能力

- `routes/session/dialog-timeline.tsx`：只以真实 user text part 构造 timeline，并可跳转到消息；
- `routes/session/dialog-fork-from-timeline.tsx` 与 session route：按选定 message ID fork Session；
- `component/dialog-session-list.tsx`：Session 搜索、当前项、更新时间、workspace/status 与选择恢复；
- Session server/SDK 是 fork 和列表的权威 owner，TUI 只是 consumer。

NZ-Coder 没有全屏 route/scroll target，因此本阶段对齐其核心语义：可辨认的用户回合、当前/历史 Session 可见性，以及不修改 workspace 的会话分支。

### 42.2 NZ-Coder 原有不足

- `/sessions` 只是格式化字符串，缺 active marker、model、mode 和 message count 表格；
- 新启动但尚未保存正文的 Session 只有 `active.json`，不在 `list_sessions()` 结果中，真实 PTY 首次冒烟会漏掉当前 Session；
- 没有 timeline，恢复或 fork 前无法知道某一历史位置对应哪个用户任务；
- `/resume` 只能整体恢复，没有安全的上下文分支；
- history 中混有 reminder、reflection、verification 等内部 `role=user` 诊断，直接按 role 计数会把它们误报为用户回合。

### 42.3 实现结果

Timeline projection：

```text
messages
  -> identify real user prompts
     (exclude known synthetic diagnostic prefixes)
  -> group until next real user prompt
  -> last assistant text + stable unique tool names
  -> bounded Rich table: Turn / User / Agent / Tools
```

新命令：

```text
/sessions       active + latest saved metadata table
/timeline [N]   last N visible user turns, 1 <= N <= 100
/fork [TURN]    default latest visible turn
```

Fork 生命周期：

```text
save current Session
  -> deepcopy history through selected complete turn
  -> allocate fork-* Session ID
  -> construct new Agent in same workspace and same permission mode
  -> success: replace owner, close old Agent, persist fork
  -> failure: reactivate old Session, preserve old Agent/history
```

`/fork` 明确只复制对话上下文：不会复制、回滚或切换 workspace 文件。它与 InfCode 的 message fork 类似，但不是 Git branch/worktree，也不能用来恢复旧文件状态；文件回退仍使用 `/undo` 或版本控制。

### 42.4 关键设计决策

1. **turn 是真实用户回合，不是 message 数组下标**：用户不应理解内部 tool/diagnostic message 排列；`/fork 3` 必须稳定对应 timeline 的第 3 行。
2. **保留 synthetic context、隐藏 synthetic turn**：内部诊断仍可能是该回合推理所需上下文，fork slice 不删除它们；只是不把它们展示成用户任务。
3. **完整回合边界**：fork 包含选中 user prompt 后直到下一真实 prompt 前的 assistant/tool/reminder，避免产生悬空 tool call/result。
4. **deep copy**：新旧 Session 的 message dict/part metadata 不共享可变引用。
5. **先创建后替换**：新 Agent 成功前不清空 history、不关闭旧 Agent；失败显式恢复 active Session。
6. **Session 列表不创建 Agent**：只读取 bounded metadata；active Session 即使尚无独立 ID 文件也从 `active.json` 合并到首行。

### 42.5 关键文件

- `nz_coder/interface/timeline.py`：ConversationTurn、synthetic 识别、timeline grouping、fork slice 和 Session table；
- `nz_coder/interface/commands/handlers/core.py`：`/timeline`、`/fork` 与结构化 `/sessions`；
- `tests/test_timeline.py`：grouping、deep copy、边界、Rich 表格和 unsaved active Session；
- `tests/test_cli_commands.py`：fork owner replacement 与构造失败回滚。

### 42.6 验证结果

- Ruff：A039 源码与测试 `All checks passed!`；
- timeline/CLI/input/run-renderer/release-doc 聚焦测试：`39 passed`；
- 真实 PTY：`/timeline` 与 `/sessions` 表格成功渲染；首次冒烟发现并修复 unsaved active Session 漏行；
- 完整回归：`769 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 没有执行真实 `/fork` 以避免无必要地改变用户当前 active Session；成功/失败路径由隔离单测覆盖；
- 没有发送模型请求、没有访问公网、没有启动 MCP server、没有运行 SWE-bench。

### 42.7 学习重点

1. Session timeline 的关键不是表格，而是把用户语义回合从协议内部 message 中稳定投影出来。
2. Agent 注入消息复用 `role=user` 是模型协议实现细节，不能直接成为 UI 的用户身份判断。
3. conversation fork 与 filesystem fork 是两个概念；CLI 必须明确文件仍共享，否则用户会误以为得到了代码快照。
4. active alias 和 durable Session ID 文件具有不同提交时机，列表 consumer 必须合并两者。
5. 任何 owner switch 都应先构造新 owner，成功后再关闭旧 owner。

### 42.8 剩余差距

- timeline 是静态表格，不能用方向键跳转、搜索或把选中历史 prompt 放回编辑器；
- Session list 没有 fuzzy search、rename/delete confirmation、按日期/workspace 分组；
- fork 只复制本地 history，不复制 InfCode 的服务端 title/share/workspace metadata；
- fork Session 与原 Session 共享当前 workspace 文件，没有 worktree isolation；
- 没有持久 parent_session_id/forked_at_turn provenance；
- 尚无全屏 sidebar 或鼠标交互。

## 43. A040：CLI 异步键盘选择与状态迁移

### 43.1 InfCode 参考能力

- `packages/opencode/src/cli/cmd/tui/ui/dialog-select.tsx` 提供统一的 option contract、当前项、键盘移动、过滤和选择回调；
- `component/dialog-model.tsx`、`component/dialog-session-list.tsx` 与 `routes/session/dialog-fork-from-timeline.tsx` 把同一选择抽象用于模型、Session 和 fork；
- TUI 选择层只产生用户选择，真正的 Session/model 生命周期仍由已有 store/SDK owner 执行。

NZ-Coder 仍是 scrolling REPL，本阶段对齐的是“一个可复用选择层驱动真实状态迁移”，不是复制 Solid/OpenTUI 组件树。

### 43.2 NZ-Coder 原有不足

- A037 只有输入补全，用户必须记住并输入完整 Session ID、provider/model 或 turn number；
- A039 虽能列出 Session/timeline，却不能直接从列表选择；
- CLI 已运行在 asyncio event loop 中，直接调用同步 dialog `run()` 会造成 nested event loop；
- 命令注册器只接受同步 handler，无法安全等待 terminal selector；
- 若 picker 另写 resume/model/fork 生命周期，容易绕过旧 Agent close、model rollback 或 fork failure restore。

### 43.3 实现结果

调用链：

```text
TerminalInput.select_async()
  -> prompt_toolkit Application.run_async()
  -> CommandRegistry.dispatch_async()
  -> /session | /model-picker | /fork-picker
  -> existing resume/model/fork transition
  -> replace Agent owner only after new owner exists
```

新增命令：

```text
/session       choose saved Session metadata, then resume
/model-picker  choose an offline-known model, then hot-switch
/fork-picker   choose a visible completed user turn, then fork
```

选择器只在交互 TTY 中启用；非 TTY 不打开全屏 application，而是报告交互终端要求。模型候选只读取 active/cache/config/registry 的离线快照，不因打开 picker 发网络请求。Session picker 复用 `session_options()`，表格和选择器因此共享同一 active/metadata 口径。

### 43.4 关键设计决策

1. **await application，不调用 `asyncio.run()`**：CLI 已有唯一事件循环，selector 是其中一个异步阶段。
2. **保留同步 dispatch**：已有测试、脚本和非交互 caller 仍可调用普通命令；异步 handler 被同步入口调用时会明确报错并关闭未等待 coroutine。
3. **selector 不拥有状态**：它只返回 value；Session/model/fork 继续走 A032/A039 已验证的 owner replacement、rollback 和 persistence。
4. **候选读取不做副作用发现**：打开 model picker 不应触发 provider `/models` 请求；需要刷新时仍由显式 `nz-coder models refresh` 完成。
5. **准确披露按键语义**：prompt_toolkit radio list 是 Up/Down 移动、Space 选中、Tab 到 OK、Enter 确认；真实 PTY 冒烟发现这一点后同步修正 `/keys`，不声称单次 Enter 即完成。

### 43.5 关键文件

- `nz_coder/interface/terminal_input.py`：awaited radio selector、TTY fallback 和 selector style；
- `nz_coder/interface/commands/registry.py`：兼容同步 handler 的 `dispatch_async()`；
- `nz_coder/interface/commands/handlers/core.py`：三个 picker 命令及既有状态迁移复用；
- `nz_coder/interface/timeline.py`：Session table/picker 共用的 metadata projection；
- `nz_coder/interface/cli.py`：主 REPL 异步 command dispatch 接线；
- `tests/test_terminal_input.py`、`tests/test_timeline.py`、`tests/test_cli_commands.py`：selector、metadata 与三类 owner transition。

### 43.6 验证结果

- Ruff：A040 源码与测试 `All checks passed!`；
- terminal input/timeline/CLI commands 聚焦：`36 passed`；
- 真实 PTY：selector 进入备用屏幕、移动焦点并通过 OK 返回候选；该冒烟同时校正了 Space/Tab 的真实操作说明；
- 完整回归：`776 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 未发送模型请求、未访问公网、未启动 MCP server、未执行真实 Session/model/fork 改写；状态成功/失败由隔离测试覆盖；
- 未运行 SWE-bench，符合用户当前“先对齐、评测流程暂不跑”的边界。

### 43.7 学习重点

1. 异步 CLI 中的 dialog 不是另一个 App 后端，而是同一终端事件循环里的临时输入 surface。
2. picker 的完成标准不只是返回字符串；它必须驱动已有状态 owner，并保留 rollback/close/persistence 不变量。
3. 列表和选择器若各自读取 Session，会逐渐产生 active、排序和消息数口径漂移；应共享 metadata projection。
4. “键盘可操作”必须以真实 PTY 按键验证，组件默认行为不能靠猜测写进帮助文本。

### 43.8 剩余差距

- 当前 radio selector 没有 InfCode `DialogSelect` 的实时 fuzzy filtering、分组、描述区和单 Enter 选择；
- Permission/Question 在 Agent 同步授权边界中，尚未复用该 async selector；强行接入前需要先把交互等待边界异步化；
- timeline 仍是静态表格，picker 不能跳转滚动位置或把旧 prompt 放回编辑器；
- Session 仍无 rename/delete confirmation，model picker 也不编辑 reasoning variant；
- 没有 mouse、theme 或全屏 persistent sidebar。

## 44. A041：CLI fuzzy selector 与单 Enter 选择

### 44.1 InfCode 参考能力

`packages/opencode/src/cli/cmd/tui/ui/dialog-select.tsx` 的关键行为不是“画一个弹窗”，而是统一：

- option 的 title/value 与可选 description/category；
- 输入查询后的 fuzzy 排序；
- 当前项随过滤重置、方向键循环移动；
- Enter 直接返回当前 value，Escape 关闭；
- 大列表只渲染可见区域。

A040 已对齐 awaited selector 和三条状态迁移，但 prompt_toolkit `radiolist_dialog` 的实际操作仍是方向键移动焦点、Space 选中、Tab 到 OK、Enter 确认，也没有 fuzzy filtering。A041 替换这一选择 surface，不改动下游 owner lifecycle。

### 44.2 NZ-Coder 原有不足

- Session/model 数量增加后只能逐项移动，不能按 ID、provider 或 metadata 搜索；
- radio focus 与 selected value 分离，帮助文本稍有不慎就会误导用户；
- 多步 Space/Tab/Enter 与 InfCode `DialogSelect` 的单 Enter 习惯不一致；
- 直接依赖 shortcut dialog 难以测试 fuzzy ranking、无匹配和可见窗口边界。

### 44.3 实现结果

新增 `FuzzySelector`：

```text
value/label options
  -> normalize query
  -> exact > prefix > substring > compact subsequence
  -> stable original-order tie break
  -> bounded 14-row window around selected item
  -> Enter returns value / Esc returns None
```

键盘行为：

```text
printable text   update query and reset selection to first match
Up/Down, C-p/C-n circular navigation
PageUp/PageDown  move one visible window
Enter            return highlighted value immediately
Esc or Ctrl-C    cancel without state transition
```

Session label 中的 ID、message count、model 和 mode，model label 中的 provider/model，以及 fork label 中的 turn/prompt 都进入同一搜索字符串。`TerminalInput.select_async()` 的外部 contract 不变，因此 `/session`、`/model-picker`、`/fork-picker` 无需复制或修改状态迁移代码。

### 44.4 关键设计决策

1. **自研小型排序，不增加 fuzzysort 依赖**：项目已有依赖边界足以用标准字符串操作实现 exact/prefix/substring/subsequence；排序规则可单测且稳定。
2. **value 与 label 分离**：UI 可以搜索富 label，但返回的仍是原始 Session ID、model ID 或整数 turn，不需要从展示文本反解析。
3. **过滤后 selection 归零**：旧索引在新结果中可能越界或指向不同对象；每次 query change 明确回到最佳匹配。
4. **固定可视窗口而非渲染全部候选**：最多投影 14 行，并围绕当前项移动，避免大量 model/Session 让终端布局无界增长。
5. **Application 接受可注入 input/output**：测试使用 prompt_toolkit pipe input 和 DummyOutput 执行真实 key binding，不只直接调用内部函数。

### 44.5 关键文件

- `nz_coder/interface/selector.py`：option contract、fuzzy score、过滤、窗口、键盘与异步 Application；
- `nz_coder/interface/terminal_input.py`：用 `FuzzySelector` 替换 radio shortcut，保留 TTY 和 async contract；
- `nz_coder/interface/commands/handlers/core.py`：`/keys` 更新为真实按键说明；
- `tests/test_selector.py`：排序优先级、no-match、wrap navigation、单 Enter 和 Esc 的 pipe-input 集成测试；
- `tests/test_terminal_input.py`：TerminalInput delegation contract。

### 44.6 验证结果

- Ruff：相关源码与测试 `All checks passed!`；
- selector/terminal input/CLI/timeline 聚焦：`42 passed`；
- 真实 PTY：完整屏幕中输入 `sec` 后只保留 `Second session`，同一次 Enter 返回 `PICKED=two`；
- 完整回归：`782 passed, 1 warning`；warning 是既有 Python 3.13 `fork()` deprecation；
- 没有发送模型请求、访问公网、启动 MCP server 或执行真实 Session/model/fork 状态改写；
- 没有运行 SWE-bench。

### 44.7 学习重点

1. fuzzy picker 的核心 contract 是 value/label 分离和确定性排序，不是弹窗样式。
2. UI key binding 既要单测，也要真实 PTY 验证；两者分别捕获状态逻辑和终端编码/焦点问题。
3. 过滤会改变列表 identity，selection 必须定义明确的 reset/clamp 规则。
4. 替换输入 surface 时保持状态 transition 不变，可以把交互风险与 owner 生命周期风险隔离开。

### 44.8 剩余差距

- 还没有 InfCode 的 category grouping、description/footer 分栏、匹配字符高亮和 mouse hover；
- Permission/Question 仍位于同步工具授权边界，尚不能直接等待该 async selector；
- Session 没有 rename/delete confirmation、日期/workspace 分组；
- timeline picker 只返回 fork turn，不能跳转或把旧 prompt 放回编辑器；
- selector 是命令触发的临时全屏界面，不是 persistent sidebar。

## 45. A042：CLI 交互完整收口

### 45.1 本次一次性收口范围

本项不再拆成单个控件阶段，而是把 A037–A041 留下的 terminal interaction 缺口一次闭环：

```text
prompt editing / completion / toolbar
session + model + fork fuzzy selection
permission once / always / reject
question single / multiple / custom / dismiss
blocking tool thread -> main event-loop selector bridge
Agent replacement rebinding
non-TTY + HTTP + sync compatibility
```

InfCode 的参考仍是统一 `DialogSelect` 与服务端 permission/question lifecycle。NZ-Coder 不复制 OpenTUI/store，而是保持本项目同步工具 handler contract，通过明确的线程/事件循环边界连接异步 terminal surface。

### 45.2 原有最后缺口

- `ToolExecutor.execute_one()`、PermissionManager 和 question handler 都是同步接口，直接等待 async selector 会 nested event loop 或死锁；
- Permission 仍走 `console.input()`，没有 InfCode 风格的 once/always/reject；
- Question 虽有编号文本输入，但没有 fuzzy、方向键、多选标记和统一取消；
- model/session owner replacement 后，新 Agent 必须重新绑定 terminal askers；
- 不能为了终端 UI 改坏 HTTP broker 的 pending/reply/timeout/abort contract，也不能破坏 SWE/同步 caller。

### 45.3 完整调用链

```text
AgentLoop async tool batch
  -> serial/guarded tool executes with asyncio.to_thread (ContextVar copied)
  -> PermissionManager or question tool calls synchronous bridge method
  -> asyncio.run_coroutine_threadsafe submits to the existing CLI loop
  -> renderer.pause()
  -> TerminalInput.select_async -> FuzzySelector
  -> once/always/reject or normalized answers
  -> renderer.resume()
  -> worker continues ToolExecutor without nested event loop
```

Permission：

- `once` 只允许当前调用；
- `always` 复用 PermissionManager 的最窄规则：bash 按命令首词 prefix，其他工具按 tool name；
- `reject`、Esc、Ctrl-C 和错误路径均保守拒绝。

Question：

- 单选支持 fuzzy option 与无匹配时的 custom answer；
- 多选用 Space toggle、Enter submit，已选值跨过滤保留；
- custom 多选允许包含空格；
- 任一问题 Esc 会 dismiss 整组，不制造伪答案；
- option description 进入 searchable label，返回值仍只使用原始 label。

### 45.4 兼容性与安全边界

1. **只改变 async AgentLoop 的执行位置**：同步 `_execute_tools`、ToolExecutor API、SWE helper 和 HTTP broker 仍保留原接口。
2. **ContextVar 自动复制**：`asyncio.to_thread` 保持 workspace、Session、tool state、question asker 与 dynamic tools 的当前执行上下文。
3. **event-loop thread fail closed**：同步 bridge 若被错误地从 owner thread 调用，不阻塞自己，Permission 返回 reject，Question 返回 dismissed。
4. **HTTP 不复用 terminal bridge**：HTTP 继续由 InteractionBroker 管理 pending ID、reply validation、timeout、abort 和 replay event。
5. **owner replacement 自动重绑**：异步 slash command 的 build wrapper 为 model/resume/fork/new Agent 重新安装 bridge；旧 owner 仍按既有逻辑关闭。
6. **renderer pause/resume 成对**：每个 selector 都在 finally 中恢复，取消和异常不会留下悬挂 Live surface。

### 45.5 关键文件

- `nz_coder/interface/interactions.py`：同步 Agent asker 到 async terminal loop 的线程安全桥；
- `nz_coder/interface/selector.py`：multiple/custom、Space toggle、单 Enter submit 和 cancel；
- `nz_coder/interface/terminal_input.py`：统一 selector 参数透传；
- `nz_coder/interface/cli.py`：初始 Agent 与替换 Agent 的 interaction binding；
- `nz_coder/runtime/loop.py`：async 串行/guarded 工具 offload，保留 ordered barrier；
- `tests/test_terminal_interactions.py`：bridge、Permission scoped always、Question tool ContextVar 端到端；
- `tests/test_selector.py`：单选、多选、custom spaces、取消与 bounded projection；
- `tests/test_runtime_context.py`：串行 barrier 不占 event-loop thread；
- 既有 `tests/test_http_service.py`：证明 HTTP pending/reply/timeout/abort 未回归。

### 45.6 验证结果

- Ruff：相关源码与测试 `All checks passed!`；
- selector/interaction/runtime/permission/question/CLI/HTTP 聚焦：`121 passed`；
- 真实 PTY：`PermissionManager(worker) -> TerminalInteractionBridge -> FuzzySelector(main loop)` 显示 `edit_file: app.py`，单 Enter `Allow once` 后返回 `ALLOWED=True`；
- 完整回归：`793 passed, 1 warning`；warning 是既有 Python 3.13 multiprocessing `fork()` deprecation；
- HTTP InteractionBroker 全套测试通过，未访问公网、未发送模型请求、未启动真实 MCP server；
- 未运行 SWE-bench。

### 45.7 CLI 收口结论

> 后续更正：该结论只证明交互链路闭环，没有证明默认视觉布局达到产品级。A044 的真实用户启动反馈发现固定 bottom toolbar 造成整屏空白，已修正并收窄本节的“收口”含义。

当前 terminal product boundary 内的交互主链路已经闭环，可按 frozen core 管理：输入、补全、状态、运行卡片、Session/model/fork、Permission 和 Question 均有真实 consumer、取消路径、状态 owner 和自动化证据。后续只处理真实使用暴露的 bug，不再为了逐项复制 InfCode TUI 文件继续扩张。

仍存在的差异是产品形态而非半成品链路：没有 persistent sidebar、mouse/theme、Session rename/delete、timeline jump/editor recall、图片/剪贴板 attachment 和 collapsible tool detail。它们只有在用户明确把目标升级为全屏 TUI/App 时再进入开发。

## 46. A043：终端产品首次启动与 wheel 发布收口

### 46.1 为什么不再继续复制 InfCode 功能

A042 后 terminal Agent 主链路已经闭环。本项按“陌生开发者能否从安装走到第一条任务”审计，而不是继续比较 InfCode 目录。审计发现两个 editable checkout 掩盖的真实发布缺陷：

1. README 要求在 workspace 复制 `.env`，但配置只读取 NZ-Coder 源码根；wheel 安装后 workspace 配置不会生效；
2. 顶层 `skills/code-review/SKILL.md` 不属于 `nz_coder` package，wheel 默认不会包含 bundled skill。

此外 CLI 只检查通用 `API_KEY`，会错误拒绝只设置原生 `ANTHROPIC_API_KEY`、`GEMINI_API_KEY` 或 Responses `OPENAI_API_KEY` 的合法配置。

### 46.2 实现结果

首次启动路径：

```text
python -m pip install .  (or pipx install .)
  -> cd trusted-repository
  -> nz-coder init
       create .env with O_EXCL + mode 0600; never overwrite
  -> edit one Provider credential
  -> nz-coder doctor
       offline secret-free readiness table/JSON
  -> nz-coder
```

配置优先级改为 shell environment > workspace `.env` > editable source-tree `.env` fallback。Provider connection inspection 与实际 adapter 使用相同映射：OpenAI-compatible 使用 `API_KEY`，Anthropic/Gemini/Responses 接受各自专用变量及已有 fallback。

`doctor` 的默认检查全部离线且有界：

- Python 版本与必需 import；
- workspace/state-directory 读写和 symlink 边界；
- active provider/model/variant 解析；
- credential presence（只显示 configured/missing）；
- HTTPS 或 loopback HTTP endpoint；
- permission mode；
- MCP merged config/trust，不启动 server；
- 最多扫描 2000 个文件的项目语言与已安装 LSP，不启动 language server；
- TTY 能力。

FAIL 阻断启动准备；WARN 表示可选能力。`--json` 服务 CI，`--strict` 可把 WARN 纳入发布门。

### 46.3 wheel 发布门

`scripts/release_smoke.py` 使用 `pip wheel --no-deps --no-build-isolation`，随后：

```text
inspect wheel
  -> CLI entry point exists
  -> doctor module exists
  -> bundled code-review SKILL.md exists
create temporary venv + install wheel
run outside source checkout
  -> python -m nz_coder --help
  -> import path must not point to source tree
  -> doctor --json parses successfully
```

本地真实结果：`release smoke passed: nz_coder-0.1.0-py3-none-any.whl`。

### 46.4 关键设计决策

1. **init 不提供 force overwrite**：配置可能含真实密钥，首次启动工具没有理由覆盖它；要替换必须由用户显式管理文件。
2. **dotenv 不覆盖 shell**：部署/CI 注入的环境变量应比 repository 文件优先，避免旧 `.env` 静默劫持当前配置。
3. **doctor 不探测网络健康**：默认诊断不能产生费用、泄露凭据或把临时网络失败误报成本地安装损坏。
4. **LSP 缺失是 WARN**：结构搜索仍可用；只在用户需要对应语言语义能力时安装 server。
5. **package data 显式声明**：源码目录存在不能证明 wheel 包含非 Python asset，release smoke 必须打开 wheel 检查。
6. **证据矩阵区分本地与外部**：Linux/Python 3.13 wheel 已证明；macOS/Windows、其他 Python 版本和公网 Provider 不能靠推断标绿。

### 46.5 关键文件

- `nz_coder/doctor.py`：offline readiness checks、Rich/JSON 输出与 exit policy；
- `nz_coder/initializer.py`：0600、O_EXCL workspace configuration；
- `nz_coder/providers/configuration.py`：secret-free Provider connection fact；
- `nz_coder/config.py`：workspace dotenv 与 shell precedence；
- `nz_coder/bundled_skills/code-review/SKILL.md`、`pyproject.toml`：wheel package data；
- `scripts/release_smoke.py`：build/inspect/install/source-external smoke；
- `docs/release-checklist.md`：可执行 release gate 与未验证矩阵；
- `tests/test_doctor.py`：credential mapping、secret-free diagnostics、dotenv precedence、private init 和 bundled asset。

### 46.6 验证结果

- doctor/CLI/hook/extension 聚焦：`45 passed`；
- Ruff 与 `git diff --check`：通过；
- 当前 workspace 真实 doctor：Python/dependencies/workspace/state/model/credential/HTTPS endpoint/permission/MCP 全 PASS；非交互 TTY 和缺 TypeScript LSP 为非阻断 WARN；
- non-editable wheel source-external smoke：通过；
- 完整回归：`802 passed, 1 warning`；warning 是既有 Python 3.13 multiprocessing `fork()` deprecation；
- 未访问 Provider/MCP 公网、未打印密钥、未运行 SWE-bench。

### 46.7 当前终端产品结论

在当前已验证的 Linux/Python 3.13 环境，NZ-Coder 已具备本地 Beta 发布所需的安装、初始化、诊断、核心运行和回归闭环。它仍不能宣称跨平台正式 GA：macOS、Windows Terminal、Python 3.9–3.12 和第三方公网协议需要相应环境或用户授权凭据形成证据。

后续默认不再扩 terminal core。优先事项只剩两类：真实外部兼容证据，以及用户已经明确暂缓的 SWE-bench 固定评测。全屏 TUI、IDE、插件市场和云控制面继续保持 consumer-driven。

## 47. A044：真实使用反馈驱动的 CLI 内联输入框

### 47.1 真实体验暴露的问题

A037–A043 的自动化测试证明了输入、补全、状态、结构化运行视图和安装链路可工作，但第一次从真实项目目录启动时仍出现明显的产品体验问题：输入提示位于 banner 下方，动态状态栏固定在终端最底部，两者之间留下整屏空白；连续按空 Enter 后还会在 scrollback 中堆叠多个 `nz-coder ❯`。

根因不是 Rich streaming renderer，而是 `prompt_toolkit.PromptSession(bottom_toolbar=...)` 的布局语义。bottom toolbar 会占用终端底边，适合全屏应用，却与 NZ-Coder 当前的滚动式 REPL 混用了两套界面模型。CLI 主循环又把空字符串当作一次已结束输入后重新 prompt，进一步放大了提示符残留。

### 47.2 实现结果

输入面改为单一滚动式内联 composer：

```text
conversation / tool cards
╭─ New request · provider/model · mode · context ─╮
│ ❯ user input                                    │
╰─────────────────────────────────────────────────╯
```

- 删除固定 `bottom_toolbar`，在 prompt message 中绘制带动态状态的顶部边框，用 `rprompt` 保持当前输入行右边界，并在提交后闭合底部边框；
- 主提示符从重复品牌名的 `nz-coder ❯` 收敛为 `❯`，品牌、模型、workspace 和 Session 仍由启动 banner 展示；
- Enter 只在 buffer 含非空白内容时提交，空 Enter 保持当前编辑器，不产生空 turn 或重复提示符；
- composer 宽度同时受真实终端与 Rich Console 宽度约束，长状态安全截断，多行 continuation 延续左边框；
- `Alt+Enter` 多行、slash/model/session/file 补全、history、fuzzy selector、非 TTY fallback 和 streaming renderer 均保持原路径。

### 47.3 关键设计决策

1. **没有改成全屏 TUI**：NZ-Coder 当前输出依赖正常终端 scrollback，工具卡片与模型文本也由 Rich `Live(screen=False)` 投影。只为输入框引入全屏 Application 会重新制造双 renderer 所有权问题。
2. **状态进入输入框标题**：模型、权限模式与 context 对编码 Agent 有运行意义；放入可截断的 composer 顶边比固定底栏更紧凑，也比裸提示符更容易识别当前输入区域。
3. **空 Enter 在 key binding 层阻止**：避免主循环已经结束 prompt 后再擦除输出，输入 surface 本身保持唯一事实源。
4. **真实 PTY 是必要发布证据**：注入 FakeSession 的单元测试无法证明终端高度、cursor 与 scrollback 行为；“功能可调用”不能等同于“产品界面可用”。

### 47.4 关键文件

- `nz_coder/interface/terminal_input.py`：终端宽度感知的内联 composer、动态状态标题、多行边界和非空提交门；
- `tests/test_terminal_input.py`：动态状态内容与空提交规则；
- `nz_coder/interface/cli.py`：继续复用既有滚动式 Agent/renderer 主循环，本次不引入第二套 UI owner。

### 47.5 验证结果

- CLI input/commands/interactions 聚焦：`38 passed`；
- Ruff：相关源码和测试 `All checks passed!`；
- 真实 PTY：启动后显示完整 `New request` 上边框、`│ ❯ exit │` 输入行和闭合下边框，无固定底栏、无整屏空白，正常输出 `Goodbye!`；
- 完整回归：`803 passed, 1 warning`；warning 仍是既有 Python 3.13 multiprocessing `fork()` deprecation；
- 未访问模型、未运行 SWE-bench。

### 47.6 学习重点与剩余差距

这次修正说明终端产品必须区分三类证据：状态机测试、伪终端行为和真人视觉体验。此前“CLI core frozen”说得过早；更准确的边界是功能主链路稳定，但真实体验暴露的布局 bug 仍应修复。

当前输入区已经具备产品可用的带边框 composer，但仍不是 Claude Code/InfCode 的逐像素复制：没有 attachment/paste card、主题系统、折叠工具详情和鼠标交互。后续只根据真实使用中的明确摩擦继续改进，不能再用测试数量替代产品体验判断。

## 48. A045：InfCode-style slash 菜单与模型/权限 picker 链路

### 48.1 InfCode 参考行为与原有误判

本项重新直接阅读本地 InfCode：

- `component/prompt/autocomplete.tsx`：只在 cursor offset 0 的 `/` 打开 slash popup，命令、description 和 alias 进入 fuzzy options；Up/Down、Ctrl+P/Ctrl+N 导航，Enter 选择；
- `component/dialog-command.tsx`：命令注册与可见 slash 投影分离，选中项触发 command action；
- `app.tsx`：`/models` 的 action 直接打开 `DialogModel`，permission auto-approve 也是可选择 action，不要求用户记忆内部参数；
- `component/dialog-model.tsx`：模型列表只承担选择，真正 model owner 仍在 local state/provider data。

NZ-Coder 此前只有 `CommandRegistry`、Completion records 和独立 picker，单元测试证明字符串存在，却没有证明首字符 `/` 在真实 composer 中启动 completion state，也没有把无参数 `/model`、`/mode` 接到 picker。因而用户看到的是“有命令实现，但没有产品入口”。

### 48.2 实现结果

slash 输入链路：

```text
line-start "/"
  -> explicit completion start, first option selected
  -> canonical commands + aliases + descriptions
  -> Up/Down filters/navigates
  -> Enter applies selected slash and submits it
  -> async command dispatcher
```

模型链路：

- `/model` 与 `/models` 无参数时直接打开 `Choose model` fuzzy selector；
- 如果 workspace 只有当前模型且没有缓存，这次用户显式 `/models` 会在线程中调用当前 Provider model discovery，写入既有无凭据 cache 后再打开 selector；
- 发现失败给出原因和 `/model PROVIDER/MODEL` 精确回退；
- `/model list|reset|PROVIDER/MODEL [VARIANT]` 保留兼容。

权限链路：

- `/mode` 无参数打开 `Permission mode` selector；
- `default`、`acceptEdits`、`plan`、`auto` 均显示实际风险语义，当前项有 marker；
- 选择后复用既有 `set_permission_mode`，状态立即投影到 composer；
- `/mode auto`、`/permission mode ...` 和 `/permission rules` 继续兼容。

### 48.3 关键设计决策

1. **对齐用户行为而非逐行复制**：InfCode 使用 TypeScript、SolidJS 和 OpenTUI，NZ-Coder 使用 Python 与 prompt_toolkit；可复用的是 trigger/option/action/picker 状态机，不是组件源码。
2. **alias 是一等菜单项**：InfCode `/models` 是用户入口；NZ-Coder 不能只让 registry 接受 alias，却不在 autocomplete 中展示。
3. **slash selection 直接执行**：当 completion 来源是 `/`，Enter 应完成并提交；`@file` completion 仍只插入文本，不意外发送整个 prompt。
4. **模型 discovery 只在显式入口发生**：普通启动和 doctor 继续离线；用户调用 `/models` 且本地确实没有候选时，才查询当前已配置 Provider。
5. **高风险 auto 明示**：picker 不把四种 mode 当成无差别字符串，`auto` 明确说明不会询问工具权限。

### 48.4 关键文件与验证

- `nz_coder/interface/terminal_input.py`：slash trigger、alias completion、纵向菜单与 slash Enter action；
- `nz_coder/interface/commands/handlers/core.py`：model/mode picker 默认 action 和按需 Provider discovery；
- `tests/test_terminal_input.py`：alias 投影与显式 completion trigger；
- `tests/test_cli_commands.py`：`/models` picker/discovery 和 `/mode` 风险选择。

验证结果：CLI input/command/interaction 聚焦 `41 passed`；Ruff 通过；受控 prompt_toolkit VT100 output 中 `/` 渲染 `/help`、`/keys`、`/model`、`/mode` 等带 description 的纵向列表；完整回归 `806 passed, 1 warning`，warning 仍是 Python 3.13 multiprocessing `fork()` deprecation；未发送真实模型请求、未运行 SWE-bench。

### 48.5 剩余差距

NZ-Coder 当前完成的是 slash/model/permission 的核心用户闭环，仍不是 InfCode 完整 OpenTUI：没有 Ctrl+P 全局 command dialog、recent/favorite model cycle、provider connect dialog、attachment、鼠标 menu 和 command category。后续必须先由真实体验确定优先级，不能再次用 registry/test existence 代替 UI 验收。

## 49. A046：真实 Ctrl+C 故障修复与全能力证据再审计

### 49.1 触发问题与根因

真实终端在 `list_directory`、`repo_map` 后按 Ctrl+C，`asyncio.run()` 先取消
主 task；`AgentLoop` 将 `asyncio.CancelledError` 继续上抛，而 CLI 只捕获了
`Exception`。由于 `CancelledError` 不属于普通 `Exception`，它穿过 REPL，最后
被 `asyncio.run()` 转成 `KeyboardInterrupt`，于是用户看到整段 traceback，进程
也直接退出。

只在 CLI 增加一个 `except` 仍然不安全。工具通过 worker thread 执行，取消
await 并不能杀掉已经运行的线程；如果界面立即返回，旧 worker 可能在几毫秒
或几秒后继续写文件。审计还发现单元素只读分支直接在 event-loop thread 执行，
以及工具结果回调异常时事务结束代码可能无法到达。

### 49.2 本轮修复的运行时不变量

取消链路现在遵守以下顺序：

```text
SIGINT / task.cancel
  -> retire 当前模型输出或停止等待新工作
  -> 已启动的同步 worker 必须 settle
  -> 活动本地写事务 rollback
  -> renderer 标记 cancelled
  -> 清除已处理的 task cancellation count
  -> 返回同一个 REPL
```

具体修复包括：

- Agent 运行、输入编辑和异步 slash command 分别捕获取消，Ctrl+C 不再结束整个 CLI；
- `runtime.async_utils.to_thread_settled()` 成为统一线程桥，Agent 工具、模型发现、
  自动记忆和异步子 Agent 不再各自裸用 `asyncio.to_thread()`；
- 写工具取消时先等待 worker 退出，再通过同一个 TransactionManager 回滚晚到写入；
- 同步与异步工具批次都在 dispatch、结果投影或回调异常时结束活动事务；
- 单个只读工具也进入 worker，避免它独占 terminal event loop；
- 取消的工具批次仍写入结束观测事件，不留下只有 started、没有 terminal 的 trace。

### 49.3 A001–A045 重新定级

本轮不再把“模块存在”或“单测通过”直接写成 InfCode parity。当前更准确的矩阵是：

| 维度 | 当前定级 | 已证实的真实消费者/边界 | 与 InfCode 的剩余差距 |
|---|---|---|---|
| runtime/workspace/事务 | **当前产品边界内核心可用** | CLI、HTTP、子 Agent 均通过 ContextVar/实例 owner；取消晚到写入可回滚 | 不是 OS sandbox；同步 Provider 无法被 Python 强杀，取消需要等 worker 返回 |
| context/compaction/memory | **核心可用，Provider 校准不足** | AgentLoop 每轮预算、超长内容落盘、anchored summary、近期完整回合与持久 memory 有调用方 | usage/tokenizer 仍非所有 Provider 精确；自动记忆是本地策略，不等同 InfCode 全部 Session 数据模型 |
| LSP/Repo Map/持久索引 | **部分对齐，可用于终端编码** | Repo Map 默认可见；LSP 可选加载；提交后诊断和 SQLite 增量更新由写事务调用 | Python 静态引用最强；其他语言依赖保守声明和已安装 LSP，无 Tree-sitter/vector/watcher 生态 |
| Provider/model | **四类协议基础可用，生态未对齐** | Chat Completions、Responses、Anthropic、Gemini 均进入 AgentLoop；model picker 驱动 owner replacement | 大量 vendor 名只是兼容协议别名；无 InfCode 级二十余 adapter/live 兼容矩阵 |
| MCP | **本地核心协议闭环，公网证据缺失** | stdio/HTTP/SSE/OAuth、trust、reconcile 和动态工具均有 runtime consumer | 未对任意公网第三方 server 做授权互操作；外部 write 无法加入本地文件事务 |
| Session HTTP | **loopback API 正确性闭环，架构未完全统一** | 标准库 client、CRUD/run/SSE/pending/snapshot/restart 有真实 loopback 测试 | CLI 仍直接拥有 AgentLoop，不是 HTTP 薄客户端；无 SDK/IDE/GUI consumer |
| 后台写 Agent | **本地第一阶段可用** | 路径 claim、并发、取消、快照冲突、父审查和事务 apply 有调用方 | daemon/thread 与进程内 manager，不是 InfCode 宿主 UI/跨进程任务服务；取消是协作式 |
| extensions | **metadata 对齐，插件 runtime 未对齐** | `extensions list/status` 能统一观察 Skill/Hook/tool pack/MCP | 不支持第三方包安装、版本协商、隔离加载/unload/marketplace |
| terminal UX | **可用的滚动 REPL，非完整 OpenTUI** | 内联输入框、slash menu、model/mode/session picker、权限/question 和 run cards 有真实交互链 | 无全屏 sidebar、附件、主题、鼠标、折叠详情和全局 command palette |
| SWE-bench | **deferred P0 证据缺口** | runner/guardrail 存在 | 没有固定版本/模型/配置的 300 Lite 一次性官方结果；按用户要求本轮不运行 |

因此，“HTTP/MCP/terminal core frozen”只应理解为当前本地产品边界的稳定基础，
不能再翻译成“与 InfCode 已完整对齐”。

### 49.4 代码与文档审计发现的非阻断债务

- source checkout（排除 vendored InfCode）中残留 13 个 `*.orig` 与 1 个
  `*.rej` 备份/冲突文件；它们未被 import，也不进入 wheel，但会降低源码可信度，
  后续应在确认不再需要人工恢复后删除；
- `BackgroundAgentManager` 使用 workspace+Session 键控的进程缓存，隔离正确但没有显式
  eviction；长寿命 HTTP 进程创建大量 Session 时需要生命周期上限；
- `config.py` 仍在 import 时直接解析若干整数/浮点环境变量，非法配置可能早于 doctor
  的结构化报告而失败；正式公开发布前应改为延迟、可诊断解析；
- release 文档此前仍写“bottom toolbar”和 `/model` 显示当前模型，已在 A046 同步为
  内联 composer 与默认 picker 行为。

这些是下一轮 P1/P2，不冒充本轮已完成项。

### 49.5 验证证据

- Ruff 全项目与 `compileall`：通过；
- 取消/CLI/runtime/memory/subagent 聚焦：`66 passed`；
- LSP/MCP/HTTP 聚焦：`121 passed`，包含真实本地进程/loopback 协议，不代表公网兼容；
- Provider/model 聚焦：`71 passed`，是本地 transport contract，不代表真实付费 endpoint；
- 全量测试因执行器长输出会话限制拆成互斥文件组：`96 + 271 + 446`，新增
  async-command 取消测试后最终为 `97 + 271 + 446 = 814 passed`；
- 离线入口：help、doctor JSON、models current、extensions list、mcp list 均成功；
- 未发送真实模型请求，未连接公网 MCP，未运行 SWE-bench。

### 49.6 本轮关键文件

- `nz_coder/interface/cli.py`：输入、slash command、Agent run 三个取消边界；
- `nz_coder/runtime/async_utils.py`：取消安全的统一同步 worker bridge；
- `nz_coder/runtime/loop.py`：工具调度、事务结束、单只读 offload 与批次观测；
- `tests/test_cancellation_safety.py`、`tests/test_cli_commands.py`：晚到写回滚、
  event-loop heartbeat、真实 cancellation count 和同 REPL 恢复；
- `README.md`、`docs/architecture.md`、`docs/release-baseline.md`：纠正产品声明。

## 50. A047：真实代码审查死循环与上下文证据保留

> A049 纠偏：本节当时将“跨调用 read episode 熔断”写成了主干改进，但它不是 InfCode `processor.ts` 的行为，且会拦截参数变化的合法调查。该 episode 熔断及后续试做的 `ModuleNotFoundError` 语义归一化熔断已撤回。本节仍有效的交付是模型感知的 context budget、最近两个真实用户回合保护与 synthetic message 区分。

### 50.1 InfCode 参考能力

本轮重新阅读了本地 InfCode 的实际主链路，而不是沿用 A006/A008 的旧结论：

- `packages/opencode/src/session/context-budget.ts`：工具裁剪保护量和最小收益都由模型窗口推导；
- `packages/opencode/src/session/compaction.ts`：从后向前统计工具输出，保护最近两个用户回合，并且只有可释放量超过最小阈值才写入 compact marker；
- `packages/opencode/src/session/processor.ts`：同名同参工具调用连续三次时进入 doom-loop 权限/阻断流程。

这里可复用的是三个不变量：当前任务的证据不能被轻量裁剪反复擦除；裁剪阈值必须与模型窗口一致；工具循环必须在执行前被识别。A049 进一步确认 InfCode 只对最近连续三次同名同参调用发起 `doom_loop` 权限，不对 `A→B→C→A` 做语义猜测。

### 50.2 真实故障与原有不足

用户在 `/home/pyh/test_nzcoder` 执行“review 一下我仓库的代码”后，真实 Session `session-20260803_174454-051658dc` 出现：

- 50 个 assistant 工具回合、105 个工具结果，最终达到 `max_turns=50`；
- `taskr/taskr/db.py` 被读取 19 次，`cron_engine/parser.py` 和 `taskr/taskr/cli.py` 各 15 次；
- tool call ID 每次均不同，排除了客户端重复重放旧调用；
- 旧工具结果大量变成 `[Earlier tool result compacted. Re-run the tool if needed.]`，证明模型在“读文件—证据被擦除—按提示重读”之间颠簸。

代码审查确认有三个相互放大的根因：

1. `micro_compact()` 使用固定约 8K token 阈值，并且只保留最近 3 条工具结果；这与当前模型 128K 窗口和已经存在的 `PromptBudget` 脱节。
2. A008 的 `RecoveryState` 只识别连续 `A→A→A`，任何不同工具或不同参数都会把 streak 清零，无法识别轮换重读。
3. 工具失败诊断、验证提醒、todo/reminder 等内部控制消息都使用普通 `role=user`，compaction、undo、memory 和“最后一条用户消息”逻辑无法区分真实用户回合与内部提示。

因此，此前“Agent Core 已对齐”的说法不准确。更严谨的结论是：主循环、工具协议、事务与 Provider 基础链路可用，但真实长工具回合的上下文保持和无进展终止在 A047 前没有闭环。

### 50.3 实现结果

新的上下文裁剪链路为：

```text
AgentLoop 每轮 preflight
  -> 根据当前 ModelCapabilities 生成 PromptBudget
  -> 识别真实用户回合（排除 synthetic control messages）
  -> 完整保护最近两个真实用户回合中的工具结果
  -> 只在更老结果超过 toolPruneProtectTokens
     且可释放量超过 toolPruneMinimumTokens 时写 compact marker
  -> 完整请求仍超 softPreflightTokens 时才进入 anchored auto_compact
```

新的循环防线为：

```text
每个 run 启动
  -> 清空连续 streak 与 read episode 计数
每次工具 dispatch 前
  -> 规范化 name + JSON arguments
  -> 连续三次相同调用：阻断
  -> 同一只读签名第三个独立 episode：阻断
     （A,A,B,A 只算两个 A episode，不误伤一次参数细化）
成功写事务提交
  -> workspace_changed
  -> 清空 read episode，允许读取修改后的文件
```

内部控制消息现在写入 `_nz_synthetic=true`，`message_schema.is_synthetic_user_message()` 同时识别新标记和旧 Session 的 XML 前缀。Context compaction、ChangeTracker undo 边界、memory 自动提取、turn budget/keep-going/frustration 判断和 CLI timeline 都只把真实用户消息当作用户回合；发送 Provider 前仍会剥离 `_nz_*` 私有字段。

### 50.4 关键设计决策

1. **保护回合，不按最后 N 条结果保护**：一次代码审查可能并行读取十几个文件，固定“最后 3 条”与任务语义无关；最近两个真实用户回合是 InfCode 已验证的稳定边界。
2. **循环统计独立 episode**：总次数会误伤 `A,A,B(细化),A`；只统计由其他调用隔开的重复片段，既能识别真实 `A→B→C→A`，又保持 A008 的兼容行为。
3. **只对声明为 read 的工具启用跨调用熔断**：写工具、Question、Permission、task 等有状态调用不能用“参数相同”等价为“没有进展”。
4. **写提交是证据代际边界**：文件成功修改后，同一 `read_file` 已可能返回新内容，必须清空跨调用计数；失败或回滚不能伪造进展。
5. **兼容旧 Session**：只增加新 metadata 不够，已有 `<reminder>` 等历史仍会被误判；中央 helper 同时保留前缀识别。

### 50.5 关键文件

- `nz_coder/state/context.py`：模型预算裁剪、最近真实用户回合保护和新的 compact marker；
- `nz_coder/runtime/recovery.py`：连续 streak 与跨调用 read episode 状态机；
- `nz_coder/runtime/loop.py`：dispatch 前阻断、写提交后重置、PromptBudget 传递与内部消息标记；
- `nz_coder/message_schema.py`：synthetic user message 的统一识别和旧会话兼容；
- `nz_coder/runtime/hooks.py`：验证、失败恢复和 todo 控制消息来源标记；
- `nz_coder/state/memory.py`、`nz_coder/state/changes.py`、`nz_coder/interface/timeline.py`：真实用户语义的下游消费者；
- `tests/test_recovery.py`、`tests/test_context_budget.py`、`tests/test_memory.py`、`tests/test_message_schema.py`：回归证据。

### 50.6 验证结果

- 静态检查：相关源码与测试 `ruff check` 通过，`git diff --check` 通过；
- 定向测试：Agent Core、Context、Memory、Message、Undo、Hooks、Timeline、Loop、Observability 与 Cancellation 共 110 项通过；
- 完整测试：`pytest -q` 为 `821 passed`，另有 1 条既有 Python 3.13 `fork()` deprecation warning；
- 真实故障重放：从保存的 105-call Session 提取原始序列，新 episode guard 会在第 7 轮首次阻断 `parser.py` 与 `db.py` 的第三个独立读取片段，而不是运行到第 50 轮；
- Provider replay 核查：所有重复调用 ID 均唯一，历史 assistant/tool 配对完整，故障归因不是 SDK 重放；
- 未运行 SWE-bench，继续遵守用户当前“先对齐功能、评测流程暂不跑”的要求；
- 未再次产生公网模型费用；真实重放为保存 Session 的确定性离线重放。

### 50.7 学习重点

1. Agent Loop 是否可靠，不能只看 tool calling 和单元测试；必须观察“模型拿到的证据是否在下一轮仍然存在”。
2. Context compaction 和 doom-loop detection 不是两个独立功能：过早裁剪会主动制造重读，循环 guard 只能止损，不能替代正确的上下文预算。
3. `role=user` 既承担协议角色又承担产品语义时，必须增加来源 metadata；否则 Session、memory、undo、compaction 会对同一消息产生不同误解。
4. 真实故障序列比理想化 fixture 更有价值。本轮若只测试 `A,A,A`，仍然无法发现 `A,B,C,A`。

### 50.8 剩余 Agent Core 差距

- 跨调用 guard 依据“同名同参只读调用”判断，不能识别参数不断变化但语义上无进展的搜索；后续应以真实失败样本设计 progress signal，不能盲目增加启发式。
- 部分 Provider 的 token 统计仍是统一估算而非服务端精确 usage/tokenizer；窗口 metadata 错误时只能使用保守 fallback。
- 达到绝对 `max_turns` 时当前会明确返回 `max_turns`，但不会额外发起一次禁用工具的强制总结请求；是否增加必须先设计 Provider/费用/取消边界。
- 本轮证明保存历史的协议配对正确，但没有用 DeepSeek、Anthropic、Gemini、Responses 的公网 endpoint 重跑同一审查任务；本地协议测试不能冒充公网互操作证据。
- InfCode 的完整 Session/Part 数据模型、插件生态与 host UI 广度仍未对齐；这些不是本次死循环修复的一部分。

## 51. A048：InfCode-style 终端产品控制面收口

### 51.1 InfCode 参考能力

本轮逐条阅读了终端交互的生产消费者，而不是继续把 slash completion 当成完整产品界面：

- `packages/opencode/src/cli/cmd/tui/component/dialog-command.tsx`：命令注册、分类、快捷键展示和 `Ctrl+P` command list；
- `packages/opencode/src/cli/cmd/tui/component/dialog-model.tsx`：Favorites、Recent、Provider 分组、收藏动作和最近模型切换；
- `packages/opencode/src/cli/cmd/tui/component/dialog-provider.tsx`：Provider 选择和凭据/OAuth 入口；
- `packages/opencode/src/cli/cmd/tui/component/dialog-theme-list.tsx`：主题选择、预览和取消语义；
- `packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx`：文本粘贴、附件、大段粘贴摘要、外部编辑器和 composer 状态；
- `packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`：工具详情显示/折叠；
- `packages/opencode/src/config/keybinds.ts`、`tui-schema.ts`：leader、model cycle、command list、paste、theme 和 mouse 配置。

这些文件共同说明：命令、模型、Provider、输入和工具视图必须共享同一份状态并回到同一个 Session REPL；只实现若干孤立 picker 不算产品闭环。

### 51.2 NZ-Coder 原有不足

A037–A045 已经有内联输入框、slash completion、fuzzy selector、Session/model/mode picker 和结构化工具卡，但仍有八个用户可见断点：

1. `/` 只能打开补全，没有独立的 `Ctrl+P` 全命令面板；
2. 命令没有 category、keybind 和 suggested metadata；
3. 模型列表是平铺集合，没有 recent、favorite 和快速轮换；
4. 没有终端内 Provider 连接流程，而且缺凭据时 CLI 直接退出；
5. 主题、mouse 和工具详情级别不可配置；
6. selector 只有键盘，没有鼠标行选择；
7. 没有一次性附件队列和大段粘贴卡片；
8. 没有 leader、外部编辑器与显式 `Ctrl+V` 文本粘贴入口。

这解释了为什么此前功能测试通过后，真实界面仍让用户觉得“不像 Claude Code/InfCode 产品”。

### 51.3 实现结果

新的输入/命令主链为：

```text
PromptSession composer
  -> Ctrl+P / Ctrl+X chord / F2 产生 TerminalInputAction
  -> 同一 asyncio REPL awaited FuzzySelector
  -> CommandRegistry.visible_commands(category/keybind/suggested)
  -> 原 CommandContext dispatch
  -> 命令完成后回到同一 composer 和 Session owner
```

具体结果：

- `Ctrl+P` 打开全屏、可搜索、带分类和快捷键提示的命令面板；slash completion 继续作为快速入口；
- 增加 `Ctrl+X M/T/N/L/G/C/S/U/R` leader 命令、`Ctrl+X E` 外部编辑器、`Ctrl+V` 应用/系统文本剪贴板和 `F2` 最近模型轮换；
- 模型 picker 按 Favorites、Recent、Provider 投影，`Ctrl+F` 原地收藏，`Ctrl+A` 进入 Provider 连接；
- `/connect` 使用掩码密钥输入，原子写入 workspace `.env` 并设为 `0600`，随后发现模型并通过原有 model switch/Agent replacement 路径即时生效；
- CLI 在无凭据时不再退出，而是进入受限 REPL，允许 `/connect` 修复配置；未连接时使用明确失败的占位 client，不能误发请求；
- live Provider 凭据使用 `ContextVar` overlay，而不是修改模块级 `config`，模型发现和 Provider factory 都消费同一隔离连接；
- `/theme`、`/mouse`、`/tool-details` 写入 workspace 私有原子偏好文件，prompt_toolkit selector、composer 和 Rich tool renderer 使用同一状态；
- selector 支持 mouse-up 直接选择；工具卡支持 hidden/compact/full 三档；
- `/attach`、`/attachments`、`/detach` 管理最多 20 个 workspace 内非 symlink 普通文件，一次提交后清空；真实用户消息收到显式 `<attached-files>` 引用，终端显示附件卡；
- 五行或 800 字符以上的文本粘贴显示行数/字符数卡片，但原文仍完整进入模型消息。

### 51.4 关键设计决策

1. **保留 scrolling REPL，不复制 OpenTUI 组件树**：NZ-Coder 当前产品是 Rich + prompt_toolkit；对齐交互不变量比移植 TS 全屏框架更小、更稳，也不会创建第二套 Session owner。
2. **所有 picker 回到原注册表和 handler**：命令面板只选择 command name，模型 picker 最终仍调用带回滚的 `handle_model()`；没有并行的演示状态。
3. **偏好是 workspace-owned**：不同仓库可以有不同主题、工具详情与模型历史，文件采用 bounded JSON、路径校验、原子 replace 和 `0600`。
4. **实时凭据是 ContextVar overlay**：写 `.env` 负责下次启动，ContextVar 负责本次 Session；二者都不修改模块级 config，也不会让并行 workspace 串 Provider。
5. **附件传引用而非伪造多模态内容**：现有 Provider message contract 是文本/工具协议，附件因此只声明已验证的 workspace 文件路径，让 Agent 使用已有安全文件工具读取；没有把二进制图片冒充已上传内容。
6. **full 仍有安全上限**：工具完整视图保留 4,000 字符输出上限，避免一个命令把终端滚动历史无限撑大；原始工具结果仍在 Session/trace 中。

### 51.5 关键文件

- `nz_coder/interface/preferences.py`：主题、mouse、工具详情、recent/favorite model 的 workspace 原子状态；
- `nz_coder/interface/terminal_input.py`：composer action、Ctrl+P/leader/F2/Ctrl+V、命令面板、附件队列和偏好热加载；
- `nz_coder/interface/selector.py`：dialog action、主题和 mouse row selection；
- `nz_coder/interface/commands/registry.py`：category/keybind/suggested/hidden contract；
- `nz_coder/interface/commands/handlers/core.py`：模型分组/收藏/轮换、connect、theme、mouse、tool-details 和 attachment 命令；
- `nz_coder/providers/connect.py`、`configuration.py`、`providers/__init__.py`：私有 `.env`、Provider 目录、ContextVar live connection 和 factory 消费；
- `nz_coder/interface/run_renderer.py`：hidden/compact/full 工具卡；
- `nz_coder/interface/cli.py`：无凭据启动、共享主题、附件/粘贴卡和生产主循环接线；
- `tests/test_terminal_preferences.py`、`test_provider_connect.py`、`test_terminal_product_alignment.py`：新增状态、安全与端到端 handler 证据。

### 51.6 验证结果

- 静态检查：新增/修改 Python 文件 `py_compile` 通过；生产模块不写 runtime config 的 AST 防线通过；
- 定向测试：terminal input、selector、renderer、commands、preferences、provider connect 和 product alignment 共 69 项通过；
- 完整测试：`pytest -q` 为 `838 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 真实 PTY：验证 `Ctrl+P` 搜索并执行 `/tool-details`、`Ctrl+X T` 打开/取消主题 picker、`/attach README.md` 更新 composer 状态、`/detach all` 清理并最终 `exit` 正常关闭；
- 记忆系统：通过真实 `save_memory` 更新 `infcode_code_alignment_contract`，随后 `recall_memory` 成功召回 A048 contract；
- 未运行 SWE-bench，继续遵守“先对齐功能、评测流程暂不跑”；未调用付费公网模型或写入真实新凭据。

### 51.7 学习重点

1. slash popup、command palette 和 command dispatch 是三层能力，不能用第一层冒充完整终端产品。
2. Provider connect 的闭环不是“写了 `.env`”，还包括无凭据可启动、密钥不回显、当前进程即时生效、模型发现和 Agent owner 安全替换。
3. UI preference 也是运行时状态；若继续放模块级变量，会重新引入 A014 已解决的 workspace 串扰。
4. 真实 PTY 会暴露 mock 看不到的 alternate-screen、键序列、边框恢复和同 REPL 返回问题，因此必须作为终端完成证据。

### 51.8 剩余差距

- NZ-Coder 是 scrolling Rich/prompt_toolkit REPL，不追求 InfCode OpenTUI 的逐像素布局；单个历史工具卡不能原地点击展开，但同一能力由全局详情档位提供。
- `Ctrl+V` 本轮只承诺文本剪贴板；图片/截图的二进制多模态上传需要先扩展 Provider message schema，不能在终端层伪造完成。
- Provider dialog 当前覆盖 NZ-Coder 已支持的 API-key Provider families 和常用 OpenAI-compatible 服务；InfCode 某些 Provider 的 OAuth/browser login 属于 Provider 认证能力，不是本轮已有 adapter 的 UI 接线。
- mouse 路径已在 selector 控件实现并有单元测试，真实 PTY 冒烟使用的是键盘；不同终端模拟器的 mouse protocol 兼容性仍需要发布矩阵。
- 公网 Provider 的真实 discovery/请求互操作未执行，因此本轮完成的是本地终端控制闭环，不把离线测试表述成第三方服务认证。

## 52. A049：InfCode 源码级 step-limit、doom-loop 与工具投影纠偏

### 52.1 InfCode 参考能力

本轮先读代码再修改，参考的生产链是：

- `packages/opencode/src/session/prompt.ts`：`step++`后以 `step >= agent.steps` 判定最后 step，在模型消息末尾追加 `MAX_STEPS`；
- `packages/opencode/src/session/prompt/max-steps.txt`：要求停止工具调用，必须用文本说明已完成、未完成和下一步；
- `packages/opencode/src/session/processor.ts`：`DOOM_LOOP_THRESHOLD = 3`，只检查最近三个 part 的工具名和完整 input 是否相同，命中后请求 `doom_loop` 权限；
- `packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`：Read/Glob/Grep 是 inline tool，Bash 有 output metadata 时是 block tool；关闭 tool details 时隐藏已成功工具。

### 52.2 NZ-Coder 原有不足与错误实现

真实代码审查运行达到 50 轮后，NZ-Coder 直接输出 `Agent stopped after reaching max_turns=50`，用户看不到审查结论。我随后没有先重读 InfCode，而是自行增加了两层启发式：

1. 对轮换参数的 read episode 做跨调用熔断；
2. 对 `ModuleNotFoundError` 根模块名做语义归一化，参数改变也可能被拦截。

这两项都不是 InfCode 的 doom-loop 合同，会把“合法改变调查方式”和“完全重复”混在一起。终端投影也一度把所有成功工具都变成无输出单行，这又丢掉了 Bash 故障证据。

### 52.3 实现结果

现在的最后 step 链路为：

```text
turn_index + 1 >= max_turns
  -> 在 Provider messages 末尾追加 InfCode MAX_STEPS assistant message
  -> 模型停止工具调用
  -> 返回已完成 / 未完成 / 后续建议文本
  -> 按普通 completed/completed_unverified 路径收口
```

doom-loop 链路改为：

```text
每次 dispatch 前规范化 tool name + arguments
  -> 参数或工具名改变：连续计数清零
  -> 连续第三次完全相同：请求 doom_loop 权限
  -> once / always：重置 streak 并执行调用
  -> reject 或无交互 host：不执行并把诊断返回模型
```

终端 compact 视图中，Read/Search/Repo Map 等成功工具只投影为带摘要的单行；Bash 存在输出时保留宽度受限的 block；错误和 full 模式仍保留输出。

### 52.4 关键设计决策

1. **以本地 InfCode 源码为行为规格**：不再把“我认为更聪明”的熔断写成对齐。
2. **最后 step 是模型收尾，不是本地报错**：InfCode 用一条高优先级 assistant message 改变最后一次请求，NZ-Coder 保持同样的 Provider message 语义。
3. **doom-loop 允许用户覆盖**：重复调用可能有意义，因此不能将阈值命中等同于绝对拒绝；异步 CLI 通过 worker 进入既有 Permission bridge，避免 event-loop 自锁。
4. **工具输出按种类投影**：Read 内容已在模型 Session 中，终端不必重复印大块；Bash 输出是用户诊断证据，不能一起隐藏。

### 52.5 关键文件

- `nz_coder/runtime/loop.py`：`MAX_STEPS` 注入、精确重复检测与同步/异步 doom-loop 权限收口；
- `nz_coder/runtime/recovery.py`：仅保留连续同名同参 streak；
- `nz_coder/tool_platform/permissioning/manager.py`：`doom_loop` 特殊权限的 once/always/fail-closed 语义；
- `nz_coder/interface/run_renderer.py`：InfCode-style inline/block 工具投影；
- `tests/test_loop_fake.py`、`test_recovery.py`、`test_permissions.py`、`test_run_renderer.py`：最后 step、参数变化、权限覆盖和 Bash 输出回归。

### 52.6 验证结果

- 聚焦测试：Permission、Recovery、Renderer、Loop 和 Terminal interaction 共 78 项通过；
- 静态检查：相关文件 `ruff check` 与 `git diff --check` 通过；
- 完整回归：`pytest -q` 为 `843 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 真实 PTY：以 `MAX_AGENT_TURNS=2 nz-coder` 启动安装后命令，DeepSeek 第一 step 调用 `list_directory`，第二 step 输出“已完成 / 目录总结 / 未完成 / 后续建议”，终态为 `Run completed`；
- 未运行 SWE-bench，继续遵守用户当前评测延后要求。

### 52.7 学习重点

1. “参考了 InfCode”和“行为与 InfCode 一致”不是一回事；必须能指到生产分支和对应测试。
2. 熔断逻辑越“聪明”，误杀边界越难证明；InfCode 选择精确重复 + 用户权限，是可预期的产品合同。
3. 真实 PTY + 真实 Provider 能验证“最后用户是否拿到答案”，这是 mock 的 request-shape 断言不能替代的。

### 52.8 剩余差距

- NZ-Coder 仍是 Rich + prompt_toolkit scrolling REPL，不是 InfCode OpenTUI 的逐像素复制；
- InfCode 的 Bash block 支持鼠标原地展开/折叠，NZ-Coder 仍以全局 hidden/compact/full 档位切换；
- 本轮真实 Provider 只验证了当前 OpenAI-compatible DeepSeek，不代表 Anthropic/Gemini/Responses 公网互操已验证。

## 53. A050：InfCode 本地终端命令面对齐

### 53.1 InfCode 参考能力

本轮直接从以下生产注册点提取 slash command 清单和 consumer：

- `packages/opencode/src/cli/cmd/tui/app.tsx`：`sessions/new/models/agents/mcps/variants/connect/status/themes/help/exit`；
- `packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx`：`editor/skills`；
- `packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`：`rename/timeline/fork/compact/undo/redo/copy/export`；
- `packages/opencode/src/cli/cmd/tui/util/transcript.ts`：Session title、role heading、thinking、tool input/output 的 Markdown 格式；
- `packages/opencode/src/cli/cmd/tui/util/clipboard.ts`：OSC 52、tmux/screen passthrough 与平台原生剪贴板 fallback。

对比结果显示 NZ-Coder 已有 Session picker/new/model/status/theme/help/timeline/fork/compact/undo/redo 后端，但八个本地能力没有统一终端入口。

### 53.2 实现结果

- `/rename [TITLE]`：无参时使用掩码之外的小型文本 dialog，有参时直接设置；标题写入 Session ID 主文件以及匹配的 active/latest alias，之后 `save_session()` 不会丢失；
- `/copy`：根据当前 history 生成 Markdown transcript，优先发送 OSC 52，再尝试 `wl-copy/xclip/xsel/pbcopy/clip`；
- `/export [PATH]`：默认生成 `session-<id>.md`，只允许 workspace 内非 symlink 目标，使用同目录临时文件 + `fsync` + replace 原子写入；
- `/skills`：读取当前 Agent-owned `SkillLoader`，显示 available/conditional、project/user/bundled 来源和描述；
- `/mcps`：合并分层 MCP 配置与当前 Agent runtime status，显示 transport/source/trust/tool count/error；
- `/variants`：从当前 `ModelCapabilities` 取得可选 variant，复用 selector 和已有安全 model owner replacement；
- `/editor`：命令完成后回到同一 composer，通过 prompt-toolkit `pre_run` 立即打开 `$VISUAL/$EDITOR`；
- `/exit` 及 `/quit`/`/q`：进入 CommandRegistry、slash completion 和 Ctrl+P，不再只是 REPL 中的特殊字符串。

### 53.3 关键安全和状态边界

1. Transcript 跳过 synthetic user messages，tool arguments 使用 JSON 展示，并动态增长 Markdown fence，避免工具输出中的反引号破坏文档。
2. Clipboard 限制 1 MB，原生工具通过 stdin 传入，不把 transcript 插入 shell command，避免命令注入。
3. Export 不允许 `..`、绝对 workspace 外路径或 symlink target，不用普通 `write_text()` 直接覆盖半个文件。
4. Session rename 修改的是展示 title，不修改稳定 session ID 或 artifact 路径。
5. MCP/Skill 面板是真实 runtime 投影，不建第二份 UI-only 状态。

### 53.4 关键文件

- `nz_coder/interface/commands/handlers/core.py`：八个命令的注册、handler 和安全边界；
- `nz_coder/interface/clipboard.py`：OSC 52 与原生剪贴板；
- `nz_coder/interface/timeline.py`：InfCode-style Markdown transcript 投影；
- `nz_coder/state/sessions.py`：Session title 持久化和 alias 一致性；
- `nz_coder/interface/terminal_input.py`、`cli.py`：外部编辑器和 registry-driven exit 主循环接线；
- `tests/test_terminal_infcode_commands.py`：命令存在、transcript fence、rename/save、export escape、OSC 52、variant/editor/exit 证据。

### 53.5 验证结果

- 聚焦测试：新命令、CLI、TerminalInput、Timeline、Smoke 和 Terminal product 共 `102 passed`；
- 静态检查：相关源码/测试 `ruff check` 与 `git diff --check` 通过；
- 完整回归：`849 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 真实 PTY：在新建临时 workspace 中完成 `/rename → /skills → /mcps → /export → /exit`；导出文档标题与 Session JSON title 一致；`EDITOR=true /editor` 返回同一 composer；
- 未运行 SWE-bench，未发起新的付费 Provider 请求。

### 53.6 未伪造对齐的外部/前端能力

- `/share`/`/unshare` 需要远程 Session share service，NZ-Coder 当前没有该后端；
- `/org` 依赖 InfCode console/provider organization 账户，NZ-Coder 的 API-key Provider 没有同等概念；
- `/agents` 切换的是 InfCode Agent profile，不能用 NZ-Coder permission mode 冒充；
- `/timestamps` 和 `/thinking` 需要在 Session message-part 投影中保留时间与 reasoning delta，当前 scrolling renderer 尚未有这两类生产数据；
- Sidebar、scrollbar、code concealment 和鼠标原地展开是 OpenTUI 全屏 frontend 能力，不在 Rich scrolling REPL 中注册空命令。

## 54. A051：InfCode Ctrl+C 清空与双击退出合同

### 54.1 InfCode 参考能力

本轮从真实故障反查以下生产链路：

- `packages/opencode/src/config/keybinds.ts`：`app_exit` 默认绑定 `ctrl+c,ctrl+d,<leader>q`，`input_clear` 同时绑定 `ctrl+c`，`session_interrupt` 独立绑定 `escape`；
- `packages/opencode/src/cli/cmd/tui/component/prompt/index.tsx`：有输入时 `input_clear` 清空 prompt；空输入时第一次 Ctrl+C 增加 `exitPress` 并显示 `ctrl+c again to exit`，一秒内第二次调用 app `exit()`；Ctrl+D 在空输入时立即退出。

这说明 Ctrl+C 的行为必须结合输入状态判断，不能在外层把所有 `KeyboardInterrupt` 永久翻译成“取消后继续”。

### 54.2 NZ-Coder 原有不足

- `cli.py` 在输入态捕获每一次 `KeyboardInterrupt` 后固定打印 `Input cancelled` 并 `continue`，不存在退出状态，因此用户无论按多少次都无法退出；
- prompt-toolkit 没有显式 `c-c` binding，无法在信号变成异常前区分“输入非空”和“输入为空”；
- `/keys` 把 Ctrl+C 只描述为取消，与已经进入 CommandRegistry 的 `/exit` 也没有形成一致的产品合同。

### 54.3 实现结果

- composer 非空时 Ctrl+C 只清空当前输入，不退出、不提交；
- composer 为空时第一次 Ctrl+C 返回结构化 `exit_press` action，显示 `Press Ctrl+C again to exit.`；一秒内第二次转成 EOF 退出主循环；
- 非交互/fallback reader 若直接抛出 `KeyboardInterrupt`，CLI 使用同一个一秒双击合同；
- Agent 正在运行时 Ctrl+C 保留 A046 的安全取消语义，只终止当前 run 并回到 REPL，不直接杀死进程；
- Ctrl+D、`/exit`、`exit`、`quit` 和 `q` 仍可直接退出。

### 54.4 关键设计决策

1. 输入态必须由 prompt-toolkit key binding 读取当前 buffer，外层异常无法可靠恢复被清空前的文本。
2. 使用 `time.monotonic()` 计算一秒窗口，避免系统时间调整导致双击判断错误。
3. `exit_press` 是输入 surface action，不伪装成用户文本或 slash command，因此不会污染 Session history。
4. Agent 运行态仍以 Ctrl+C 取消当前任务，是 NZ-Coder scrolling REPL 的现有安全边界；InfCode 的独立 Escape interrupt 依赖持续活动的 OpenTUI input surface，当前未伪称已经复制。

### 54.5 关键文件

- `nz_coder/interface/terminal_input.py`：Ctrl+C buffer 判断、结构化 action、一秒双击状态；
- `nz_coder/interface/cli.py`：fallback `KeyboardInterrupt` 双击退出和运行中取消边界；
- `nz_coder/interface/commands/handlers/core.py`：`/keys` 用户合同；
- `tests/test_terminal_input.py`、`tests/test_cli_commands.py`：非空清空、空输入 action、窗口过期和 fallback 退出测试。

### 54.6 验证结果

- 聚焦测试：TerminalInput、CLI、Terminal product/interaction 共 `58 passed`；
- 静态检查：相关文件 `ruff check` 与 `git diff --check` 通过；
- 完整回归：`854 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 真实 PTY：启动已安装 `nz-coder`，第一次空输入 Ctrl+C 显示再次退出提示，一秒内第二次输出 `Goodbye!`，进程退出码为 0；
- 未运行 SWE-bench，未调用付费 Provider。

### 54.7 学习重点

1. 同一个按键可以同时属于输入编辑、应用退出和运行取消三个状态机；只在最外层捕获异常会丢失输入状态。
2. “Ctrl+C 不崩溃”不等于“终端交互已对齐”；必须验证用户能看到退出提示并真的退出。
3. 真实 PTY 对控制字符、时序窗口和进程退出码的证据不可由 handler 单测替代。

### 54.8 剩余差距

- NZ-Coder 运行中使用 Ctrl+C 取消 Agent；InfCode OpenTUI 使用 Escape 的双击 interrupt 提示。若后续引入常驻全屏 input event loop，需要再对齐运行态 Escape，而不是在 asyncio run 外层读取裸 Escape。
- 当前提示打印在 scrolling REPL 的 composer 之间，不是 OpenTUI 同一帧内的动态 footer。

## 55. A052：InfCode 核心结束条件与模型 Trace 对齐

### 55.1 InfCode 参考能力

本轮针对真实 463 秒只读审查 trace，重新读取生产链而不是按 NZ-Coder 旧测试修补：

- `packages/opencode/src/session/prompt.ts`：runLoop 检测到当前 assistant 已有 finish、finish 不是 `tool-calls`、没有待处理工具并回答当前 user 后直接退出；不存在“先验证再结束”或“自动反思后再结束”的隐式模型调用；
- `packages/opencode/src/session/processor.ts`：processor 在真实 stream event 上调用 `noteToken()`，记录 first-token latency，并由 step/turn metrics 收口模型生成；
- `packages/opencode/src/session/context-budget.ts`：输出 reserve、85% soft preflight、25% tool prune protect 与 10% minimum 均从模型窗口统一派生；
- `packages/opencode/src/session/compaction.ts` 与 `prompt.ts` preflight：超软阈值时先 prune 旧 tool output、再降级大输入；不是通过 verification/reflection 多跑模型来管理上下文。

源码搜索确认 InfCode Session/Agent/Tool 生产链中没有 NZ-Coder 的 `verification_gate` 或默认 `reflection` completion gate。

### 55.2 NZ-Coder 原有不足

真实只读请求“review 一下代码，只审查不修改”暴露了以下偏差：

- 模型第 13 次调用已经给出 4389 字最终审查，但默认 `verification_gate_hook` 因前面失败的探测命令连续重开 4 次；
- gate 耗尽后默认 `reflection_gate_hook` 又启动一个 12 轮、23 工具的 reflection 子 Agent，额外耗时 94.6 秒；
- 整轮 463.0 秒中主模型等待 357.4 秒、工具仅 10.7 秒，但旧 `/trace` 只汇总工具耗时，用户看不到主因；
- `TRACE_ENABLED=0` 与 AgentLoop 实际强制开启 trace 相互矛盾，环境配置不能控制生产行为；
- LLM trace 只有 request/response 时间点，没有本轮 duration、TTFT 或 retry attempts。

### 55.3 实现结果

- `build_default_hooks()` 不再注册 verification/reflection completion gate；assistant 第一次返回无工具最终文本即结束，和 InfCode runLoop 一致；
- VerificationManager 继续记录测试/静态检查证据并进入最终 status/trace，但不再擅自替模型增加回合；模型仍通过 system prompt 被要求在代码修改后运行针对性验证；
- reflection 子 Agent 和两个 legacy gate 保留为显式 opt-in API，不再是每个生产 Agent 的隐式尾调用；`NZ_REFLECTION_ENABLED` 默认关闭；
- core trace 默认开启但现在尊重 `TRACE_ENABLED=0`；
- `LLMResult` 携带整次模型调用 duration、首个 text/reasoning/tool stream event 的 TTFT 和 attempts，`llm_response` 持久化这些字段；
- `/trace` 汇总模型调用数、总/平均/最大等待、TTFT、首个/最大输入 token 估计、子 Agent foreground span 和其余开销；对 A052 之前没有 duration 字段的 JSONL，按顺序配对 request/response 时间戳回算；
- NZ-Coder 已有 PromptBudget 与 InfCode 的 85%/25%/10% 窗口比例一致，本轮没有为了“看起来有改动”重写已对齐的上下文模块。

### 55.4 关键设计决策

1. **结束条件服从 Provider finish，而非本地自创评分器**：代码质量要求进入 prompt、permission 和工具反馈，不能在模型明确结束后偷偷再调用模型。
2. **证据记录不等于强制续跑**：失败测试仍是用户可见的重要证据，但只读审查发现失败正是有效结果，不应被解释成 Agent 必须修复。
3. **Reflection 必须显式**：需要 critic 时由模型调用 `task(agent_type=reflection)` 或外部 consumer 注册 hook；不能让每个普通请求默认付出另一个 Agent 的成本。
4. **TTFT 取第一个有效 stream delta**：text、reasoning 或 tool-call 任一首次到达都算 Provider 已开始响应，与 InfCode processor 的 `noteToken()` 意义一致。
5. **旧 trace 可诊断**：没有新字段的历史事故仍能从 request/response 时间戳回算模型等待，避免可观测性升级后旧证据失效。

### 55.5 关键文件

- `nz_coder/runtime/hooks.py`：生产默认 no-tool finish 合同，legacy gate 改为 opt-in；
- `nz_coder/runtime/loop.py`：LLM duration/TTFT/attempts 采集和 response trace；
- `nz_coder/state/trace.py`：模型/上下文/子 Agent phase 汇总与旧 trace 回算；
- `nz_coder/config.py`：trace 默认与禁用语义、reflection 默认关闭；
- `tests/test_hooks.py`、`tests/test_loop_fake.py`、`tests/test_observability.py`：首次 finish、显式 legacy hook 和模型 trace 回归。

### 55.6 验证结果

- 聚焦测试：Hook、Loop、Observability、Verification、Session events 共 `116 passed`；Context/Recovery/Permission/Model capability 共 `59 passed`；
- 静态检查：相关文件 `ruff check` 和 `git diff --check` 通过；
- 完整回归：`855 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 历史 trace 重放：旧 463.0 秒运行被新版摘要还原为 26 次模型调用/357.4 秒、39 次工具/10.7 秒、reflection/94.6 秒、输入 2367→72728 tokens；
- 真实同题 PTY：14 次模型调用后第一次无工具最终回答立即完成，未出现 `verification_gate`、`reflection_review` 或 `subagent_spawn`；模型等待 157.9 秒，TTFT 平均 1.52 秒，输入 2367→40686 tokens；
- 真实总时长 291.3 秒中有 133.3 秒记在 Bash 工具 span，主要是 default permission picker 的人工等待；本轮按用户要求未继续改 CLI/权限交互；
- 未运行 SWE-bench。

### 55.7 学习重点

1. 质量 gate 如果没有参考实现依据，即使测试很多，也可能把“发现失败”错误解释成“必须继续修复”。
2. 对 Agent 性能必须分解模型、工具、权限等待和子 Agent；只显示工具卡毫秒数会把最大瓶颈藏起来。
3. “自动 reflection 提升质量”不是免费增强：它改变结束语义、成本和延迟，必须由用户或明确 consumer 选择。
4. 已对齐的上下文预算应保留；本轮慢的主因是多余回合，不是 85% preflight 公式缺失。

### 55.8 剩余差距

- 当前 ToolExecutor duration 包含 permission picker 等待，尚未拆成 `permission_wait_ms` 与实际 handler duration；这是下一轮 core observability 的明确缺口；
- DeepSeek 在真实审查中仍自行进行了 14 次模型往返和多次相似 pytest 探测。移除隐式 gate 后核心不再额外续跑，但 Provider prompt/tool selection 仍需继续按 InfCode system/agent prompt 源码核对；
- NZ-Coder streaming trace 已有 TTFT，但 CLI 文本仍为避免工具前置草稿而缓冲；用户已要求本阶段先不处理 CLI。

## 56. A053：InfCode 上下文压缩、持久指令与记忆边界

### 56.1 InfCode 参考能力

本轮不是看到 `context.py` 和 `memory.py` 就判断“已经对齐”，而是重新读取以下生产链：

- `packages/opencode/src/session/context-budget.ts`：从模型窗口与输出 reserve 统一派生 usable input、85% soft preflight、15% input expansion、25% tool prune protect 和 10% prune minimum；
- `packages/opencode/src/session/prompt.ts`：send-time preflight 超过 soft threshold 时只持久 prune 旧工具输出、降级自动展开内容并重新估算；清理后仍高于 soft threshold只告警，不在这里创建摘要轮；
- `packages/opencode/src/session/overflow.ts`：自动 compaction 依据上一条完成 assistant 的 Provider usage token 是否越过 usable input，而不是只靠字符估算；
- `packages/opencode/src/session/compaction.ts`：默认优先保留最近两个人类 turn，保留预算为 usable input 的 25% 且限制在 2K–8K tokens；完整 turn 放不下时允许从 turn 内消息边界保留 suffix；summary 使用 anchored 模板，并在成功后持久化 tail boundary 和 tool compact marker；
- `packages/opencode/src/session/message-v2.ts`：`filterCompactedEffect()` 根据已完成 summary 和 `tail_start_id` 恢复 summary 后可见历史，工具输出的 `time.compacted` 进入持久消息状态；
- `packages/opencode/src/infcode/session/input-expansion.ts`：只裁剪系统自动展开的 file/folder/diff/terminal/skill/MCP/editor context，用户自然语言不在 expansion budget 中；
- `packages/opencode/src/infcode/session/instruction.ts`、`instruction-budget.ts`、`instruction-files/*` 和 `rules/*`：全局/项目 `AGENTS.md`、`CLAUDE.md` 与一级 Markdown rules 进入 20KB/源、32KB 总预算，并以 user-side `<system-reminder>` 注入；项目源优先于全局源；
- 对 `packages/opencode/src` 的 memory/long-term/MEMORY.md 搜索没有发现语义检索式长期记忆生产链。InfCode 此处的持久连续性来自 Session 数据、compaction summary 和 instruction/rule 文件，不能把 NZ-Coder 自研语义 memory 冒充为 InfCode 同构能力。

### 56.2 NZ-Coder 原有不足与对 A052 的纠正

A052 当时只确认了比例公式，因而写了“上下文预算已经对齐，本轮无需重写”。更深的 producer-to-consumer 对照证明这句话只适用于预算数值，不适用于触发顺序和恢复语义：

- NZ-Coder 在完整请求估算超过 85% soft threshold 后立即多调用一次 summary model；InfCode 的 soft threshold 只做清理，真正 compaction 由 hard/usage overflow 触发；
- tail 仍由固定 `32_000` 字符决定，summary 输入仍由固定 `80_000` 字符决定，不随 64K/128K/更大模型窗口变化，也不能在 oversized recent turn 内保留安全 suffix；
- Provider 响应的 `usage` 没有归一化、落入 assistant history 或参与下一轮 overflow 判断；
- Agent 运行时完全不读取根 `AGENTS.md`、`CLAUDE.md` 或 project rules；当前 Codex 能看到仓库 `AGENTS.md` 不代表 NZ-Coder 自己能看到；
- CLI/HTTP 构造 system prompt 时静态注入一次 memory index，AgentLoop 每轮又召回一次，既重复又可能过期，还会使 query-dependent memory 破坏 system prompt cache；
- 自动记忆在 `default`/`acceptEdits` 权限模式被直接跳过，把“是否允许自动修改代码”错误地等同于“是否允许写本地 Agent metadata”；
- 进入 memory pipeline 前丢失 `_nz_synthetic`，内部控制消息可能被误当成用户事实；session 压缩后只按 `last_message_count` 续游标，历史缩短时可能跳过真正的新消息。

### 56.3 实现结果

上下文调用链现在是：

```text
active model capability
  -> PromptBudget 统一派生 soft/hard/prune/tail 预算
  -> 超大自然语言仅在 hard limit 无法容纳时落盘并留可读引用
  -> micro_compact 持久替换最近两个人类 turn 之前的旧工具结果
  -> 完整 request（system + tools schema + instructions + history）重新估算
  -> soft < request <= hard：记录 preflight 告警，直接发送，不调用 summary model
  -> request > hard 或 last assistant Provider usage >= hard
  -> anchored summary + 模型预算 recent tail；必要时从 assistant 安全边界保留 turn suffix
```

具体变化：

- `LLMResult` 归一化 OpenAI/Anthropic 风格 input/output/total usage；streaming 在有 usage chunk 时采集，non-streaming 从 response 采集；usage 以 `_nz_usage` 留在内部 assistant history，发送 Provider 前仍由 `_sanitize_messages()` 剥离；
- `compact` trace 记录 `provider_usage` 或 `request_estimate` 触发原因；soft preflight 和工具裁剪分别记录 before/after token estimate；
- compaction recent-tail 预算改为 usable input 的 25%，下限 2K、上限 8K；summary head 最大输入也由模型 usable budget 派生，并保留 20K token 安全上限；
- 新增 `state/instructions.py`，发现并预算全局 `~/.config/nz-coder/{AGENTS.md,CLAUDE.md,rules/*.md}` 与项目根 `AGENTS.md`、`CLAUDE.md`、`.nz-coder/rules/*.md`；项目 instruction 优先占累计预算，最终仍按 global→project 顺序渲染；
- instruction reminder 每轮从磁盘重新读取，因此修改后无需重启；它进入 user-side context，不固化进 base system prompt。NZ-Coder 没有为了 scope label 强制依赖 Git；InfCode 的 Git probe 只用于“是否 checked in”标签，不是指令生效条件；
- CLI/HTTP 不再静态复制 memory index；每轮只按当前用户查询召回一次，并把结果与 runtime/scratchpad 一起作为低优先级动态 context；
- 自动 memory pipeline 对所有 permission mode 生效，但仍受 `MEMORY_AUTO_EXTRACT` 开关控制；快照保留 message ID 和 synthetic provenance；session state 保存最近 512 个 processed message keys，压缩/裁头后仍能识别新消息；旧 count-only state 自动迁移；
- `<session-summary>` 和 synthetic user control messages 不参与长期记忆提取。

### 56.4 关键设计决策

1. **Soft preflight 不是 compaction trigger**：85% 是腾挪空间和告警线；把它当硬线会平白增加一次模型调用并丢失精确历史。
2. **优先真实 usage，估算负责兜底**：Provider 返回 usage 时采用 InfCode 的上一轮 overflow 语义；兼容端点不返回 usage 时，仍用包含 tool schema、system、instruction 的完整估算守住窗口。
3. **指令不等于记忆**：`AGENTS.md`/rules 是用户维护的约束；semantic memory 是可能过时的背景知识。两者必须有不同优先级、预算和提示措辞。
4. **不伪造 InfCode 长期语义记忆**：NZ-Coder 的 Markdown/可选 store、相关性召回、自动提取和 dream consolidation 是明确的 NZ-only enhancement；对齐点是 Session/summary/instruction 的边界，不是相同文件名。
5. **不为标签强制 Git**：项目规则是否有效与 Git 无关；当前只标为 project instructions，避免又把版本控制变成运行前提。
6. **message ID 是压缩后的记忆游标**：消息数量会因 compaction 减少，稳定 identity 才能表达“这条是否已经处理”。无 ID 的旧输入使用内容 hash+occurrence 兼容。

### 56.5 关键文件

- `nz_coder/state/context.py`：模型预算、旧工具裁剪、hard-limit 持久化、recent-tail/split 与 anchored summary；
- `nz_coder/runtime/loop.py`：usage 采集/持久化、soft/hard 触发顺序、instruction 注入、动态 memory 和 message identity；
- `nz_coder/state/instructions.py`：全局/项目 instruction/rule 发现、优先级、UTF-8 byte budget 与 reminder；
- `nz_coder/state/memory.py`：synthetic-safe snapshot、稳定处理游标、压缩 summary 过滤与自动提取；
- `nz_coder/interface/cli.py`、`nz_coder/http_service/manager.py`：移除启动时静态 memory 重复注入；
- `tests/test_context_budget.py`、`tests/test_instructions.py`、`tests/test_memory.py`：soft/hard、usage overflow、turn split、instruction 优先级与压缩后 memory cursor 回归。

### 56.6 验证结果

- 首轮 Context/Instruction/Memory 聚焦：`28 passed`；
- Loop/HTTP/Message/Scratchpad/Architecture/Observability 回归：`107 passed`；
- 最终 Context/Instruction/Memory/Loop/HTTP/Provider/Observability 聚焦：`145 passed`；
- 静态检查：相关文件 `ruff check` 与已跟踪文件 `git diff --check` 通过；
- 完整回归：`861 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 本轮未运行 SWE-bench，未调用付费 Provider。usage streaming 是否返回取决于具体兼容端点；无 usage 时的 estimate fallback 已覆盖。

### 56.7 学习重点

1. 相同的 85%/25%/10% 常量不等于相同的上下文系统；真正决定成本和信息损失的是触发顺序、持久 marker 与恢复边界。
2. “仓库里有 AGENTS.md”与“Agent runtime 消费了 AGENTS.md”是两回事，必须沿 producer→budget→prompt consumer 验证。
3. 长期记忆最危险的错误不是搜不到，而是把内部控制提示、旧任务状态或重复静态索引当成高优先级事实。
4. Session compaction 会让数组位置失去语义；任何跨压缩游标都应建立在稳定 ID 上。
5. 自研增强可以保留，但面试说明必须诚实区分：InfCode parity、架构翻译和 NZ-only enhancement。

### 56.8 剩余差距

- 本节完成时尚缺 input-expansion part metadata；该差距已由 A054 为当前真实 `/attach` 与行内 `@file` 入口补齐；
- 本节完成时尚缺结构化 compaction/tool marker；A054 已增加 summary boundary/archive 与 tool compacted time，但 NZ-Coder 仍使用 JSON Session+archive，而不是复制 InfCode 的 SQLite message/part 表；
- tokenizer 仍是本地 CJK/ASCII 估算，只有 Provider 返回 usage 的已完成轮能精确校准；不同 Provider 的流式 usage 支持需用公开端点逐项互操作验证；
- semantic memory 的自动提取/合并质量是 NZ-Coder 自有能力，尚无长期真实使用 precision/recall 数据；不能用 861 项单测声称其知识质量已验证；
- 全局 instruction 采用 NZ-Coder 自己的 `~/.config/nz-coder` 产品目录，而不是复用 InfCode `~/.infcode`；这是品牌与配置隔离，不是文件路径级复制。

## 57. A054：InfCode input-expansion 与 compaction marker 持久化

### 57.1 InfCode 参考能力

本轮直接复用 A053 已读取的生产源码，并再次核对关键消费者：

- `packages/opencode/src/infcode/session/input-expansion.ts`：`tag()` 保存 kind/source/original bytes/tokens；`applyBudget()` 只处理带 `metadata.input_expansion` 的系统展开内容，单个超限内容截断、多个内容从后向前保留，放不下的写 tombstone；`compactStored()` 在 preflight/overflow 后持久降级，绝不修改用户自然语言；
- `packages/opencode/src/session/prompt.ts`：file/folder/diff/terminal/skill/MCP/editor context 的生成端负责补标，在 send-time soft preflight 调用 persistent prune 和 `compactStored()`；
- `packages/opencode/src/session/message-v2.ts`：`CompactionPart` 保存 `auto/overflow/resume/tail_start_id`；tool part 的 `time.compacted` 决定未来请求只看到 cleared marker；`filterCompacted()` 使用完成 summary 与 tail boundary 选择模型可见历史；
- `packages/opencode/src/session/compaction.ts`：summary 成功或 fallback 后持久写入 `tail_start_id`，然后 prune 旧工具输出；原始 message/part 仍在存储层，模型只消费过滤后的视图。

### 57.2 NZ-Coder 原有不足

- `/attach` 只把“请用 read_file 读取这些路径”的列表拼进用户字符串，没有读取内容，因此 A053 文档里提到的 input-expansion 仍没有生产者；
- 终端 completion 支持 `@src.py`，但提交后它仍只是普通字符，completion 与 Agent runtime 没有闭环；
- 用户自然语言、附件说明和未来可能的展开内容混在同一个 `content` 字符串，无法只裁附件而保留原问题；
- `micro_compact()` 只把 tool content 替换成一句 marker，没有 compacted timestamp；
- summary 只有 XML-like 文本，没有结构化 `auto/overflow/tail_start_id` part；压缩前 transcript 以秒命名，极端情况下同一秒连续压缩会覆盖 archive；
- HTTP WithParts projection 只支持单 text part，无法向客户端表达 compaction boundary 或 input-expansion metadata。

### 57.3 实现结果

input-expansion 生产链现在是：

```text
/attach PATH 或用户输入 @workspace/file
  -> TerminalInput workspace/symlink/regular-file 校验
  -> CLI user message 写 _nz_user_text + _nz_input_expansions
  -> Agent preflight 按模型 expansionBudgetTokens 解析文件
  -> 单文件超限：保留开头 + read_file offset/limit 提示
  -> 多文件超限：从后向前保留；其余写 actionable tombstone
  -> 只重建 system-expanded-context，用户自然语言原样保留
  -> 若完整请求仍超过 soft threshold，compactStored 持久 tombstone expansion
  -> Session JSON 与 WithParts text metadata 保存 source/bytes/tokens/flags/reason
```

具体能力：

- 新增 `state/input_expansion.py`，支持 InfCode 的七类 kind schema、readable-source tombstone、UTF-8 有界读取、单 expansion truncate、multiple later-first、持久 preflight compaction 和幂等 `budgetApplied`；
- 当前真实 producer 覆盖 `/attach` 和行内 `@file`；不存在或逃逸 workspace、symlink、目录及不可读 source 不会被读取，只会生成 tombstone；
- `_nz_user_text` 与 `_nz_input_expansions` 分离，oversized natural-language persistence 更新自然文本后再重建 expansion，因此不会被下一轮 resolve 撤销；
- `_sanitize_messages()` 继续剥离所有 `_nz_*` 内部字段，Provider 只看到预算后的公开 `content`；
- tool result 被 micro-compact 时写 `_nz_tool_compacted_at`，WithParts text part 投影为 `time.compacted`；
- summary message 写 `_nz_compaction`，包括 auto/overflow/resume、tail start ID、head message IDs、创建时间和压缩前 archive；message projection 新增真实 `compaction` part；
- transcript archive 改用 `time_ns` 唯一文件名，避免连续 compaction 覆盖；Session JSON 保存 marker，测试证明 save→load→WithParts 后 boundary 仍存在；
- message part 规范化从“强制只留一个 text part”升级为 text+compaction 的 additive schema，同时继续拒绝非法 ID、路径式 ID 和未验证 metadata。

### 57.4 关键设计决策

1. **复制行为，不复制 TypeScript 类型**：InfCode 的 `MessageV2.Part` 映射成 NZ-Coder 内部 `_nz_*` metadata 与 additive WithParts；Provider Chat Completions 仍接收标准 role/content/tool_calls。
2. **自然语言永远不进入 expansion budget**：附件再大也只能截附件；用户问题只有整条自然输入本身超过 hard limit 时才走 A053 的持久文件引用。
3. **实际入口先于空 schema**：先接 `/attach` 和已经存在的 `@file` completion；没有 editor selection、@diff composer 或 MCP resource mention consumer 时不注册伪入口。
4. **持久降级必须幂等**：`budgetApplied` 防止同一附件每个 Agent step 再截一次、重复追加 truncated note。
5. **JSON+archive 是架构翻译**：NZ-Coder 没有 InfCode SQLite message/part 表，因此用唯一 archive 保存被移出的完整历史、用 summary marker 保存边界；这提供恢复证据，但不声称存储引擎同构。
6. **metadata 投影不泄漏展开正文**：WithParts metadata 只包含 kind/source/size/token/flags/reason，正文仍只在 text/content 与本地 Session 中，不复制到 metadata。

### 57.5 关键文件

- `nz_coder/state/input_expansion.py`：tag、resolve、budget、truncate、tombstone、persistent compact 与 render；
- `nz_coder/interface/terminal_input.py`：`/attach` 与 inline `@file` 的 workspace-safe producer；
- `nz_coder/interface/cli.py`：把一次性附件写入 user message expansion metadata；
- `nz_coder/runtime/loop.py`：每轮 resolve、soft-preflight compact、trace 与 auto-compaction marker；
- `nz_coder/state/context.py`：oversized natural text/expansion 协作、tool compacted time、唯一 transcript archive 和 summary boundary；
- `nz_coder/message_schema.py`：text+compaction parts、input-expansion metadata 与 compacted time 的验证/投影；
- `tests/test_input_expansion.py`、`tests/test_context_budget.py`、`tests/test_message_schema.py`、`tests/test_terminal_input.py`：预算、安全、幂等、archive、Session round-trip 和真实入口回归。

### 57.6 验证结果

- 首轮 expansion/context/message/terminal/loop 聚焦：`129 passed`；
- 核心/CLI/HTTP/Memory 组合回归：`177 passed`；
- inline `@file` 补齐后终端/expansion/context/message 聚焦：`51 passed`；
- 最终结构 schema/context 聚焦：`25 passed`；
- 静态检查：相关文件 `ruff check` 与 tracked diff check 通过；
- 完整回归：`869 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 本轮未运行 SWE-bench，未调用付费 Provider。

### 57.7 学习重点

1. input-expansion 必须同时有 producer、metadata、预算器、持久降级和 Provider consumer；只有一个 `input_expansion.py` 文件不构成能力。
2. 自动展开最重要的安全边界不是截断长度，而是“哪些字允许系统删除”：只能删除系统生成内容，不能误删用户自然语言。
3. UI completion 如果没有提交/运行时 consumer，只是提示外观，不是产品能力。
4. compaction marker 的价值在恢复与审计；没有唯一 archive 或 stable ID，结构化 boundary 也无法指向真实历史。
5. additive message projection 可以逐步接近 InfCode contract，同时保持 Chat Completions 和旧 Session 向后兼容。

### 57.8 剩余差距

- 当前生产 producer 是 file attachment/`@file`。InfCode IDE 还会产生 folder、git diff、terminal context、skill body、MCP resource 和 editor selection expansion；NZ-Coder 没有对应 composer/editor consumer 的类型不应伪造已接入，后续只有出现真实入口时接到同一模块；
- InfCode 将 compacted head 保留在同一数据库 message/part 表并由 `filterCompacted()` 动态过滤；NZ-Coder 将 head 保存在唯一 transcript archive，活跃 history 使用 summary+tail。信息可审计恢复，但不支持在同一 Session API 中任意切换 pre-compaction view；
- 二进制/图片附件当前按 UTF-8 replacement 文本处理，不等同 InfCode media/document part 与 vision provider 能力；在引入真实 media consumer 前应继续用文件工具而不是声称支持多模态 expansion；
- Provider 端 streaming usage 的公开互操作证据仍属于 A053 剩余项。

## 58. A055：Agent Core step/tool/retry 持久状态机

### 58.1 InfCode 参考能力

本轮不再按模块名称判断 Agent Core，而是直接沿生产链阅读：

- `packages/opencode/src/session/prompt.ts`：在每个 Agent step 创建 assistant message，并由 processor 的 `stop/continue/compact` 结果决定下一步；
- `packages/opencode/src/session/processor.ts`：请求开始前记录 snapshot/step-start，流式维护 reasoning/text/tool part，并把 pending、running、completed、error、interrupt 和 step-finish 持久化；
- `packages/opencode/src/session/message-v2.ts`：ToolPart、RetryPart、StepStartPart、StepFinishPart 是 Session 真数据，不只是 UI 临时事件；Provider projection 根据 part 终态重建下一轮消息；
- `packages/opencode/src/session/retry.ts`：context/auth 等错误不重试，429/5xx/网络错误按连续失败预算重试，尊重 `retry-after-ms` 和 `retry-after`；
- `packages/opencode/src/session/system.ts` 与 `session/prompt/*.txt`：按模型 prompt family 选择 Anthropic、Codex、Gemini、Kimi 等生产合同，而非给所有模型同一份提示。

### 58.2 NZ-Coder 原有不足

- Session event bus 能显示 tool started/completed，但 assistant `_nz_parts` 只有 text/compaction；Session 恢复后工具生命周期消失；
- tool call 只有独立的 OpenAI-style assistant/tool message，没有同一 assistant step 下 pending→running→terminal 的持久状态；
- 模型调用取消发生在 assistant message 写入前，取消轮次从历史中完全消失；
- API recovery 对 400/422 之外的异常基本统一重试，可能对 auth/context 错误浪费回合，也不消费 Provider Retry-After；
- Kimi 被错误归到 default prompt family；其他 family 只有一句 appendix，远弱于 InfCode 的生产约束。

### 58.3 实现结果

主循环现在采用以下持久序列：

```text
build provider request
  -> create durable assistant identity
  -> persist step-start before provider call
  -> stream text events / collect reasoning and tool calls
  -> persist retry decisions while waiting
  -> persist reasoning + tool pending
  -> tool dispatch boundary: pending -> running
  -> result: completed(output) or error(error/interrupted)
  -> persist step-finish(reason + normalized usage)
  -> next Agent step
```

具体变化：

- 新增 `runtime/session_processor.py`，以稳定 message/part ID 管理 step、reasoning、tool 和 retry；同一 tool call/attempt 更新同一 part，不追加重复状态；
- `message_schema.py` 的 additive schema 扩展为 text、reasoning、compaction、step-start、step-finish、tool、retry，并严格校验状态、时间、usage、call ID 和公开字段；
- assistant draft 在 Provider 请求前进入 history 并 checkpoint；成功、400/422 diagnostic、重试耗尽和 asyncio cancel 都必须产生 step-finish；空失败 assistant 会被 Provider sanitizer 过滤，不污染下一次标准请求；
- tool dispatch 前整批转 running，结果消费时转 completed/error；用户取消或执行异常把所有未结算工具转成 `error + interrupted`，丢弃半截输出；
- Session 在 step-start、tool-running、tool-terminal、step-finish、retry 等边界 best-effort 原子 checkpoint；HTTP 最终 settlement 仍是 authoritative commit；
- `RecoveryState` 增加 transient 分类和 Retry-After seconds/date/ms 解析；401/403、context overflow、invalid key/usage cap 不再盲重试；
- retry part 在等待开始前写入 attempt/message/next，Trace 同步记录 retryable、wait 和 resumed time；
- Kimi capability 改为 `prompt_family=kimi`；Anthropic/Codex/Gemini/GPT/Kimi/Qwen/default 的 appendix 改为从 InfCode 生产 prompt 提炼的模型特定执行合同。

### 58.4 关键设计决策

1. **先保持 Provider wire contract**：内部使用 MessageV2-like parts，发送模型前仍投影为现有 Chat Completions/Responses/Anthropic/Gemini 标准消息，避免破坏已有 adapter 和 SWE-bench 入口。
2. **失败 turn 也必须存在**：取消、diagnostic 和 retry exhaustion 是真实 Session 事实；用空 public content 加内部 error/step parts 保存，Provider view 过滤空 assistant。
3. **事件不是存储**：SSE `session.tool.*` 继续服务实时 UI，但恢复依据是 message parts 和 Session checkpoint，不能用 event card 冒充 durable state。
4. **Prompt 做架构翻译**：不能逐字复制 InfCode 品牌链接、不存在的 Read/Edit/Task 工具名和 OpenTUI 指令；本轮复制 family selector 与行为合同，再映射到 NZ-Coder 已注册工具。
5. **旧内部调用保持兼容**：SWE/harness 可直接借用 `_execute_tools_async`；processor 参数为可选，旧 harness 不必实现新 helper 或消费新关键字。

### 58.5 关键文件

- `nz_coder/runtime/session_processor.py`：持久 Agent step/tool/retry 状态机；
- `nz_coder/runtime/loop.py`：请求前 assistant draft、checkpoint、取消/失败终态和工具生命周期接线；
- `nz_coder/message_schema.py`：七类 additive parts 的验证与 Session/HTTP 投影；
- `nz_coder/runtime/recovery.py`：错误分类、Retry-After 和 backoff；
- `nz_coder/providers/capabilities.py`：InfCode-style prompt family 选择与模型合同；
- `tests/test_session_processor.py`、`tests/test_loop_fake.py`、`tests/test_session_events.py`、`tests/test_http_service.py`、`tests/test_recovery.py`：持久状态、真实 Loop、SSE、取消恢复和 retry 回归。

### 58.6 验证结果

- 首轮 Message/Loop/Event/HTTP/Context/Recovery/Provider 聚焦：`163 passed`；
- cancellation harness 兼容修复后，取消/HTTP/Loop/processor/retry/provider 聚焦：`146 passed`；
- 静态检查：相关文件 `ruff check` 与 Python compile 通过；
- 完整回归：`875 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 58.7 学习重点

1. tool started/completed 事件存在，不代表 tool state 已进入可恢复的 Agent history。
2. Agent Core 的正确边界是请求前 draft 到所有 part 终态；只保存成功响应会让取消和 Provider 故障成为历史黑洞。
3. retry 是消息生命周期的一部分，不能只在控制台打印 sleep；恢复和诊断需要知道 attempt、原因和下一时间。
4. 代码级对齐需要保留旧 wire API，同时把内部状态语义翻译为参考实现的持久 part，不应为了类型同名破坏已工作的 Provider。

### 58.8 剩余差距

- 本节完成时 tool input 仍要等完整 call；该缺口已由 A056 补齐到当前 Provider delta 边界，详见第 59 节；
- InfCode 每个 step 前后保存真实 workspace snapshot 并支持 message-level revert；本轮 step schema 可保存 snapshot ID，但 NZ-Coder 尚未为普通主 Agent 实现逐 step workspace snapshot/revert，仍主要依赖写批次 TransactionManager 与 ChangeTracker；
- reasoning 已持久化，但部分 Provider adapter 只返回聚合 reasoning，没有统一 reasoning-start/delta/end 事件；
- prompt family 已按 InfCode selector 和核心合同接入，但尚未完成逐段 prompt/tool-description diff；品牌链接、InfCode 专属 GUI/Task 工具指令不会复制；
- InfCode Processor 能在 Provider stream 内执行工具并逐事件落盘；A056 已补齐逐 delta 持久化，但 NZ-Coder 仍在完整 assistant 响应后进入本地调度器；
- 本轮只证明状态机与兼容回归，不代表 SWE-bench 分数与 InfCode 等价。

## 59. A056：Agent Core 流式 message/tool-input 持久化

### 59.1 InfCode 参考能力

本轮继续沿 A055 的同一生产链下钻，不把 adapter 文件数量当成完成证据：

- `packages/opencode/src/session/processor.ts`：消费 Provider stream 的 text/reasoning/tool-input start、delta、end 事件，在流仍打开时更新 Message Part；
- `packages/opencode/src/session/message-v2.ts`：pending ToolPart 同时保存可解析 input 和尚未闭合的 raw input，终态再投影给下一轮模型；
- `packages/opencode/src/session/prompt.ts`：processor 是 assistant message、工具执行和下一 step 的状态 owner；异常与 abort 必须结算未完成 part；
- NZ-Coder 对应 Provider 生产端：`providers/anthropic.py`、`providers/openai_responses.py` 已把原生事件归一化为 OpenAI-shaped `delta.tool_calls`；OpenAI-compatible 直接提供该 delta；Gemini SDK 当前按完整 function call chunk 提供。

### 59.2 NZ-Coder 原有不足

A055 只在完整 assistant response 返回后调用 `register_tool_calls()`。因此实时 SSE 虽能显示后续工具执行，Session 真值在模型生成参数期间仍看不到 pending tool；若连接在 JSON 参数闭合前断开，历史中也没有这个未完成调用。text/reasoning 同样主要在 provider 聚合完成后进入 durable assistant，实时事件和可恢复状态存在时间差。

### 59.3 实现结果

当前流式生产链为：

```text
provider normalized delta
  -> AgentLoop accumulates text/reasoning/tool arguments
  -> SessionProcessor updates stable text/reasoning/tool part
  -> best-effort Session checkpoint while stream is open
  -> complete response reconciles by call ID or tool index
  -> dispatcher changes the same ToolPart pending -> running -> terminal
```

具体变化：

- `_call_streaming()` 每次 text delta 都更新 assistant public content 与同一个 TextPart，每次 reasoning delta 更新同一个 ReasoningPart；原有增量 SSE 保持不变，不重复广播完整文本；
- tool-call delta 一到即创建 pending ToolPart，累计 `raw` 参数；JSON 闭合后同步填入结构化 `input`；
- Provider 暂未给 call ID 时使用 `pending-{index}`，完整 call 到达后按 index 将同一 part 对账为真实 call ID，不产生重复卡片或历史；
- assistant response 接受后的 `register_tool_calls()` 仍作为最终权威校准，确保执行参数使用完整、未截断的 provider 内容；
- stream 网络失败、retry、KeyboardInterrupt 和 asyncio cancel 都会把 pending/running part 结算为 error；interrupted 路径明确丢弃半截输出；
- `MessageV2`-like schema 保存 tool index，供跨 delta 稳定关联；raw 只服务持久投影边界，实际 dispatch 不从可能受 schema 限长的 raw 字段取参数。

### 59.4 关键设计决策

1. **在共同消费边界归一化**：现有三个流式 adapter 已输出相同 `delta.tool_calls` 形状，继续在 AgentLoop 消费端持久化，避免再造一套与 adapter 重复的事件协议。
2. **index 只负责流式关联**：真实 call ID 到达后立即替换 provisional ID；执行、结果和下一轮 Provider wire 仍以真实 ID 为准。
3. **实时事件不取代 Session 真值**：增量 SSE 用于 UI，累计 part 用于恢复；两者共享 delta producer，但职责不同。
4. **保留完整响应后的调度屏障**：本轮没有在参数尚未闭合时执行工具。这样先完成持久输入语义，同时不破坏既有并行副作用调度、权限和事务边界。

### 59.5 关键文件

- `nz_coder/runtime/session_processor.py`：流式 text/tool part、provisional ID 对账和未结算工具收尾；
- `nz_coder/runtime/loop.py`：Provider delta 到 durable Session checkpoint 的接线；
- `nz_coder/message_schema.py`：ToolPart index 的验证与持久投影；
- `tests/test_session_processor.py`：partial JSON、late call ID、稳定 part 和 text upsert；
- `tests/test_session_events.py`：真实 Loop 中 pending 早于 tool dispatch，以及 partial stream retry 的持久终态。

### 59.6 验证结果

- processor/event 定向：`25 passed`；
- Session/Loop/message/HTTP/native Provider/Responses 核心聚焦：`130 passed`；
- 静态检查：相关文件 `ruff check` 与 Python compile 通过；
- 完整回归：`880 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 59.7 学习重点

1. “流式输出”至少有两层：给 UI 的瞬时 delta 和给恢复系统的累计 durable state；只有前者不算 Agent Core 闭环。
2. tool call ID 可能晚于参数流出现，index 是必要的临时关联键，但不能成为执行协议的最终 identity。
3. pending part 必须在 retry/cancel/error 上进入终态，否则 Session restore 会永久显示一个不存在的运行中工具。
4. adapter 已归一化时，应在统一 consumer 补持久语义；为追求文件形式相似再复制事件层只会制造两个真值源。

### 59.8 剩余差距

- InfCode processor 在同一 Provider stream 中驱动 tool execution；NZ-Coder 仍等完整 assistant response 后进入本地副作用调度器，因此尚未对齐“边生成后续内容、边执行工具”的统一 stream control flow；
- Gemini SDK 当前给出完整 function call chunk，能进入相同 durable 路径，但上游不提供更细的 argument token 粒度时无法伪造更早的 partial input；
- 每个 delta 的 Session checkpoint 优先保证崩溃可见性，对高 token-rate 长响应可能产生较多本地 I/O；未来可做有界 debounce，但不能丢失 start/terminal 边界；
- 本节完成时普通主 Agent 尚无每 step 的真实 workspace snapshot 与 message-level revert；该缺口已由 A057 完成第一阶段；
- 本轮不代表完整 Agent Core 或 SWE-bench 分数与 InfCode 等价。

## 60. A057：Agent Core step workspace snapshot/revert

### 60.1 InfCode 参考能力

本轮阅读的是 snapshot 的完整 producer→storage→consumer 链，而不只看 StepPart 的 `snapshot` 字段：

- `packages/opencode/src/session/processor.ts`：在 LLM stream 前预先 `Snapshot.track()`，step-start 保存起点；finish-step 再 track 终点，并生成 patch part；
- `packages/opencode/src/snapshot/index.ts`：在独立数据目录维护自己的 Git index，而不是修改或要求用户仓库 Git；track、patch、restore、revert 和 diff 都按 workspace instance 隔离；
- `packages/opencode/src/session/revert.ts`：idle 时定位 message/part 边界，收集后续 patch，先保存撤销前 snapshot，再恢复文件并记录 revert metadata；unrevert 恢复撤销前状态；cleanup 才删除被回滚的消息/part；
- `packages/opencode/src/session/summary.ts`：使用最早 step-start 与最后 step-finish snapshot 计算 Session 范围 diff；
- `packages/opencode/src/kilocode/snapshot/track.ts`：慢仓库不会无限阻塞，会显示进度、允许继续等待或禁用 snapshot。

### 60.2 NZ-Coder 原有不足

A055 的 StepStartPart/StepFinishPart 虽有 `snapshot` schema，但生产 Loop 从未填入真实 ID。现有 ChangeTracker 只保存一次用户 run 内被注册写工具触达的文本文件，`/undo` 按 change-set 和最近 user index 回滚；它无法表达一个多 step assistant 的精确边界，也无法覆盖本地外部写工具。HTTP 的 `snapshot` 是消息 reducer/cursor snapshot，与 workspace 文件快照不是同一种能力。

### 60.3 实现结果

新增的生产链为：

```text
assistant draft
  -> start workspace capture concurrently with Provider generation
  -> persist step-start immediately
  -> before any local tool dispatch: await bounded capture
  -> attach stable start snapshot to the same StepStartPart
  -> execute permission / transaction / tool lifecycle
  -> capture end workspace and persist on StepFinishPart
  -> /undo or explicit message ID
     -> preflight every affected path against finish snapshot
     -> atomic finish -> start transition + history truncation + revert state
  -> /redo
     -> validate history has not advanced
     -> atomic start -> saved pre-revert transition + history restore
```

具体变化：

- 新增标准库 `WorkspaceSnapshotStore`，按 SHA-256 manifest/blob 内容寻址保存文件；用户项目是否初始化 Git 不影响 snapshot；
- snapshot 状态放在 Session runtime 下且 workspace 扫描排除 `.nz-coder`、VCS、依赖/构建缓存、symlink 和超大文件，避免递归保存内部状态或越界读取；
- manifest ID 由 canonical file entries 计算，加载时重新校验 ID、路径、blob hash、size 和 mode；blob 读取时再次验 hash；
- transition 只触碰 source/destination 有差异的路径，支持新增、修改、删除与 executable mode；写前对所有路径统一预检，任一内容或 mode 与记录终态不同就拒绝整批恢复；
- 文件应用使用同目录原子 replace；中途异常通过临时 backup 逆序恢复。revert metadata 落盘失败时进一步反向 transition，并把 history tail 放回，避免文件和会话分叉；
- `SessionReverter` 支持默认最近真实 user turn或指定 durable message ID；revert state 保存 start/finish/recovery snapshot、精确 files 和 history tail；conversation 前进后拒绝 unrevert；
- `AgentLoop.revert_message()`/`unrevert_message()` 是核心 consumer，终端 `/undo`/`/redo` 优先使用 message snapshot，旧 Session 无完整 snapshot 时兼容回退 ChangeTracker；
- start capture 与模型生成并行，因为 NZ-Coder Provider 不在 stream 内执行工具；但任何本地 tool dispatch 前必须 await。超过 1 秒的慢仓库 capture 会取消并让该 step 明确无 snapshot，而不是延迟首 token 或留下半个 manifest；
- content blob 采用原子写并由 manifest/hash 校验，避免每文件 `fsync`；manifest 和实际 workspace restore 仍 fsync。真实 NZ-Coder workspace 10,928 个文件的首次 capture 从约 4.7 秒降至 0.67 秒，未修改增量 capture 约 0.21 秒。

### 60.4 关键设计决策

1. **不要求用户 Git**：InfCode 的本质是不污染用户仓库的独立 snapshot store。NZ-Coder 用标准库内容寻址实现同一隔离合同，符合项目无新增依赖和非 Git workspace 要求。
2. **pre-tool 而非阻塞 TTFT**：InfCode 必须在 stream 前完成，因为其 AI SDK 可能在 stream 内执行工具；NZ-Coder 本地调度明确发生在完整 call 后，因此 capture 可以与文本生成并行，但 dispatch 屏障不能越过它。
3. **冲突时宁可拒绝**：message revert 不能覆盖用户或并行 Agent 在 step-finish 后的编辑；所以先验证全部 affected paths，再做任何写入。
4. **manifest 是 commit point**：blob 缺失或损坏只会使 snapshot 不可恢复；不会把未经校验的内容写回 workspace。实际文件恢复与 revert state 仍保持耐久原子边界。
5. **保留 ChangeTracker**：它继续负责本次 Agent 改动 diff、事务风险和旧 Session 兼容；workspace snapshot 增加 step/message 级时间边界，不替换其所有消费者。

### 60.5 关键文件

- `nz_coder/runtime/workspace_snapshot.py`：有界内容寻址 capture、manifest integrity、diff 和冲突安全 transition；
- `nz_coder/runtime/session_revert.py`：message range 定位、history/revert state 与 unrevert；
- `nz_coder/runtime/loop.py`：start capture/dispatch barrier/finish capture 和公开 revert consumer；
- `nz_coder/runtime/session_processor.py`：late snapshot 附着到稳定 StepStartPart；
- `nz_coder/state/sessions.py`：Session snapshot artifact 路径；
- `nz_coder/interface/commands/handlers/core.py`：新 snapshot undo/redo 与旧 ChangeTracker fallback；
- `tests/test_workspace_snapshot.py`、`tests/test_session_revert.py`、`tests/test_session_events.py`、`tests/test_http_service.py`：文件状态、冲突、完整性、持久失败、真实 step part 与取消/二次运行时序。

### 60.6 验证结果

- workspace/session revert 定向与 Session/Loop/CLI/HTTP/cancellation 核心聚焦：`158 passed`；
- snapshot 修改后的最终定向：`29 passed`；
- 静态检查：相关文件 `ruff check`、Python compile 和 `git diff --check` 通过；
- 真实本项目 capture：10,928 files，首次约 `0.67s`，同状态增量约 `0.21s`；
- 完整回归：`889 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 60.7 学习重点

1. StepPart 有 snapshot 字段不等于 snapshot 能力存在；必须有 capture、完整性校验、差异恢复、history consumer 和失败回滚。
2. 用户仓库 Git 与 Agent 私有 snapshot backend 是两个概念；前者不应成为后者的产品前提。
3. 文件恢复与 conversation 截断是一个逻辑事务。只成功其中一边，会让模型记忆和磁盘状态互相矛盾。
4. 有界扫描一旦被截断，就不能把未收录文件解释为“不存在”；本实现直接令该 capture 失败，避免危险推断。
5. crash durability 可以分层：manifest 和 workspace transition 必须耐久；内容 blob 可用 hash 验证并在缺失时安全失败，从而避免首次扫描被数千次 fsync 拖垮。

### 60.8 剩余差距

- InfCode 使用独立 Git index，可高效处理大型仓库、gitignore 与树对象；NZ-Coder 当前是全 workspace stat walk + 进程内 digest cache，新进程首次 capture 仍需读取所有纳入范围的文件；
- 单文件超过 8 MiB、总纳入内容超过 256 MiB、超过 50,000 个文件或 capture 超过 1 秒时，该 step 不提供 revert；当前会写 trace，但尚无 InfCode 式交互进度/“继续等待或禁用”问题卡；
- 当前 message-level core API 支持指定 message ID，终端 `/undo` 默认最近 user turn，尚未增加 TUI timeline 中选中任意 message/part 的交互入口；
- 没有独立 PatchPart 和基于 snapshot 的 Session summary diff；ChangeTracker 仍承担用户可见 diff。这不影响 restore，但还不是 InfCode 完整 message/patch 投影；
- unified in-stream tool execution 仍未完成；Provider stream 与工具调度仍是两个阶段；
- 本轮不代表 SWE-bench 分数与 InfCode 等价。

## 61. A058：Agent Core 统一 tool result/control outcome

### 61.1 InfCode 参考能力

本轮重新核对了“工具在 stream 内执行”的准确含义：

- `packages/opencode/src/session/prompt.ts` 的 `resolveTools()` 把包含 `execute(args, options)` 的 Tool 对象交给 AI SDK，并在 execute wrapper 中完成 permission、plugin before/after、MCP 和 abort 接线；
- `packages/opencode/src/session/processor.ts` 的 `process()` drain 同一条 AI SDK event stream，按 `tool-input-start/delta/end → tool-call → tool-result/tool-error` 更新同一个 ToolPart；
- `tool-call` 只在完整结构化 input 到达后转 running；参数流期间的 partial input 只用于 UI，不提前执行；
- `tool-result` 转 completed，`tool-error` 转 error；Permission/Question/Suggestion rejection 会设置 `blocked`，默认 `process()` 返回 `stop`；普通工具错误不 blocked，返回 `continue` 供下一次模型调用修正；
- `experimental.continue_loop_on_deny=true` 可改变拒绝后的 stop 行为；
- `cleanup()` 等待短暂的在途工具，随后把所有残留 call 结算为 `error + interrupted`，再持久化 assistant；
- 外层 `prompt.ts` 消费 processor 的 `continue/stop/compact`，而不是仅凭“response 中是否有 tool_calls”无条件继续。

因此，“同一 stream”来自 AI SDK 可执行 Tool abstraction，不是模型还在生成半截 JSON 时就启动工具，也不是模型和本地工具任意并发。

### 61.2 NZ-Coder 原有不足

A056 已把 tool argument delta 持久化，A055 已保存 pending/running/terminal，但控制流仍固定为：只要模型返回 tool_calls，执行完后就进入下一次 LLM step。ToolExecutor 只提供 `dispatch_failed`，没有区分 permission rejection 与 invalid JSON/工具异常；用户明确 reject、Plan mode deny 或 doom-loop reject 仍会被作为普通工具错误发回模型，造成一次用户未授权的额外请求。SessionProcessor 虽保存状态，却不拥有 InfCode 的 blocked/continue/stop 决策。

### 61.3 实现结果

当前生产链变为：

```text
Provider tool input delta -> pending ToolPart
complete tool call        -> running ToolPart
ToolExecutor result
  -> permission_denied? explicit typed fact
  -> SessionProcessor.settle_tool(result/error)
  -> update the same ToolPart terminal state
  -> processor.process_result()
       denied + default       -> stop
       denied + explicit flag -> continue
       ordinary tool error    -> continue
AgentLoop consumes outcome
  -> stop: step already finished, finalize blocked, no second LLM request
  -> continue: append tool result and begin next model step
```

具体变化：

- `ToolExecutionResult` 增加 `permission_denied`，权限规则 deny、用户 reject、doom-loop reject、pre-tool guard reject 和按返回合同产生的 `Denied` 与普通 `Error:` 分离；
- `SessionProcessor.settle_tool()` 成为 tool-result/tool-error 的统一持久 consumer，负责 completed/error part，并在 denial 时设置 processor blocked；
- `SessionProcessor.process_result()` 实现 `compact/stop/continue` 优先级合同；本轮生产路径使用 stop/continue，compact 仍由现有 Context 主链决定；
- `_consume_dispatched_tools()` 不再分别调用 complete/fail helper，而是把 typed result 交给 processor；同时保留 tool message、hook、trace、transaction 和大输出持久化消费者；
- sync/async tool batch 均返回 processor outcome，并记录 `step_processor_result` trace；
- `_run()` 遇到 stop 时以 `blocked` 收口并返回同一 REPL/Session，不再发起第二次 Provider 请求；所有 ToolPart 和 StepFinishPart 已在此之前 checkpoint；
- 新增 `NZ_CONTINUE_LOOP_ON_DENY=0`，默认对应 InfCode `continue_loop_on_deny !== true`；显式设为 1 时，denial tool message 仍可交给模型继续；
- invalid JSON、tool not found、普通 handler Error 和 command nonzero 不标记 permission denial，保持可修复反馈循环。

### 61.4 关键设计决策

1. **复制行为边界，不伪造 AI SDK API**：OpenAI-compatible Chat Completions 只接受 JSON schema，不能接收 Python `execute()` callback。NZ-Coder 保留 Provider→本地 scheduler 两段物理接口，但让它们进入同一 durable processor outcome 合同。
2. **拒绝是控制事实，不是字符串错误**：虽然工具 handler 继续遵守字符串返回接口，Executor 必须把 authorization rejection 提升为 typed field，否则 Loop 无法可靠决定 stop。
3. **先结算再 stop**：拒绝不会直接抛出并跳过历史；ToolPart error、tool message、StepFinish、Session checkpoint 全部完成后才把控制权交还用户。
4. **普通失败继续**：schema/dispatch/command 错误是模型可修复反馈；只有用户/规则拒绝默认改变 processor control flow。
5. **并行批次整体 drain**：同一 response 的已调度 sibling calls 按现有副作用屏障安全结束，再由 blocked outcome stop；不能在一个线程 reject 时遗留其他 running part。

### 61.5 关键文件

- `nz_coder/runtime/tool_executor.py`：typed permission denial；
- `nz_coder/runtime/session_processor.py`：tool result/error consumer、blocked state 和 process outcome；
- `nz_coder/runtime/loop.py`：batch outcome、trace 与 stop/continue 外层消费；
- `nz_coder/config.py`、`.env.example`：continue-on-deny 生产配置；
- `tests/test_session_processor.py`：denial stop 与 explicit continue；
- `tests/test_loop_fake.py`：用户拒绝不产生第二请求、doom-loop stop 和 opt-in continue；
- `tests/test_permissions.py`：permission denial 与普通 tool error 分类。

### 61.6 验证结果

- SessionProcessor/Loop/权限/取消/Observability/Plan 聚焦：`82 passed`；
- 加入 Session event、HTTP 后的核心聚焦：`138 passed`；
- Provider/HTTP/终端补充回归：`88 passed`；
- 静态检查：相关文件 `ruff check` 与 Python compile 通过；
- 完整回归：`893 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 61.7 学习重点

1. InfCode 的 inline tool execution 是 AI SDK Tool abstraction 的能力，不等于用半截 JSON 提前执行，也不等于下一轮 LLM 请求消失。
2. durable ToolPart 和 control outcome 必须由同一个 processor 拥有；否则 UI 显示 rejected，Loop 却可能继续自动请求。
3. permission rejection 与 tool failure 对 Agent 的含义不同：前者是用户/策略边界，后者是可供模型修正的运行证据。
4. stop 必须发生在 cleanup/checkpoint 后，才能同时满足取消安全、Session restore 和终端交互。

### 61.8 剩余差距

- NZ-Coder 使用 OpenAI-compatible/原生 Provider 客户端加本地 ToolExecutor，无法像 InfCode 的 AI SDK 一样让 Provider stream 原生产生 `tool-result/tool-error` 事件；当前是语义和状态 owner 对齐，不是 SDK 内部实现同构；
- `process_result(needs_compaction=True)` 已有合同，但 context overflow/compaction 仍由 AgentLoop 原有路径消费，尚未统一为一个完整 processor `process()` 返回链；
- InfCode 对 Question dismiss、Suggestion dismiss 和 provider-executed tool 有独立 metadata 语义；NZ-Coder Question/MCP/Provider 当前没有完全相同的 typed result metadata；
- Provider-executed tools、attachments 和 tool metadata 增量更新未对齐；
- A057 的 PatchPart/Session summary diff 仍未完成；
- 本轮不代表 SWE-bench 分数与 InfCode 等价。

## 62. A059：Agent Core reactive context-overflow compact outcome

### 62.1 InfCode 参考能力

本轮读取了以下真实 producer-to-consumer 路径：

- `packages/opencode/src/session/processor.ts`：正常 `step-finish` usage 超限以及 Provider 抛出的 `ContextOverflowError` 都设置 `needsCompaction`；stream drain 在该状态出现时停止，cleanup 后 `process()` 返回 `compact`；
- `packages/opencode/src/session/prompt.ts`：外层消费 `compact`，先用 `InputExpansion.compactStored(..., reason: "context-overflow")` 持久降级系统展开输入，再执行 compaction guard、创建 auto/overflow summary 并继续 run loop；
- `packages/opencode/src/kilocode/session/prompt.ts`：`MAX_COMPACTION_ATTEMPTS = 3`，第四次仍溢出时保存 `ContextOverflowError` 并以 error 结束，避免无限循环；
- `packages/opencode/src/session/compaction.ts` 与 `packages/opencode/src/infcode/session/input-expansion.ts`：summary 和 synthetic expansion 是不同持久对象；自然用户文本不能被 tombstone。

### 62.2 NZ-Coder 原有不足

A058 虽已有 `SessionProcessor.process_result(needs_compaction=True) -> "compact"`，但生产 Loop 没有消费它。`_call_streaming()` 和 `_call_non_streaming()` 先用通用 `_is_client_error()` 捕获所有 400/422，再统一生成“tool JSON 无效”的 `<api-error-diagnostic>`。因此 Provider 的 context-window rejection 会被错误解释，并在 history 未缩小的情况下再次发给模型；这既无法恢复，也可能重复消耗 turn。

### 62.3 实现结果

当前主请求恢复链为：

```text
Provider exception
  -> narrow context-overflow classifier
  -> LLMResult(needs_compaction=True, compaction_error=...)
  -> finish failed assistant step as context-overflow
  -> SessionProcessor.process_result(needs_compaction=True)
       -> compact
  -> persistently tombstone only synthetic input expansions
  -> create overflow compaction summary + archive/marker
  -> replace active history with summary + bounded recent tail
  -> continue the same Agent run
```

具体行为：

- 新增窄范围 Provider overflow classifier，识别 `context_length_exceeded`、maximum context length、context window exceeded、prompt/input too long 等常见真实返回；普通 invalid JSON 仍走原诊断修正链；
- `LLMResult` 增加 `needs_compaction` 和 `compaction_error`，streaming/non-streaming 两条 Provider 路径产生相同 typed outcome；
- AgentLoop 在失败 assistant 上持久化 `context-overflow` StepFinish，然后显式调用 processor compact outcome，不再绕过 Session 状态 owner；
- overflow 恢复先调用既有 `compact_stored(..., "context-overflow")`，只降级 `_nz_input_expansions`，不修改用户自然语言；
- `_compact_messages(..., overflow=True)` 生成真实 summary、唯一 transcript archive 和 `_nz_compaction` marker；成功后继续当前 run；
- 每次 run 最多执行三次 reactive compaction；第四次仍溢出时保存明确 exhaustion error 并结束；
- compaction 自身发生异常时保存 error/checkpoint/trace 后结束，不再让未捕获异常击穿 Agent/CLI。

### 62.4 关键设计决策

1. **先分类具体 overflow，再处理通用 400**：HTTP 状态不足以决定恢复策略；同为 400，tool JSON 错误应反馈模型，context overflow 必须先改变 history。
2. **typed outcome 穿过 Provider/processor/Loop**：不能靠解析 `<api-error-diagnostic>` 文本触发压缩，否则状态持久化和控制决策仍有两个真值。
3. **失败 step 也要结算**：Provider 拒绝发生后仍保存 StepFinish、error 和 checkpoint；summary 替换 active history，但完整失败过程保留在 transcript archive。
4. **只 tombstone synthetic expansion**：用户自然语言即便很大，也不应被后台静默改写；summary input selector 可以跳过无法容纳的单条内容。
5. **三次 guard 与 InfCode 一致**：三次是 run 内恢复上限，不是 Provider retry；context overflow 不进入 transient backoff。

### 62.5 关键文件

- `nz_coder/runtime/recovery.py`：Provider context-overflow 窄分类与 retry policy 共用事实；
- `nz_coder/runtime/loop.py`：typed LLM result、stream/non-stream producer、processor compact consumer、summary resume 和 guard；
- `tests/test_recovery.py`：overflow 与普通 bad request 分类边界；
- `tests/test_loop_fake.py`：non-stream 完整恢复、stream typed outcome 和三次 exhaustion guard；
- `nz_coder/state/input_expansion.py`、`nz_coder/state/context.py`：复用 A054 已有持久 tombstone 与 overflow summary producer。

### 62.6 验证结果

- Recovery/SessionProcessor/Context/Loop 定向：`82 passed`；
- HTTP/Provider/Message schema/Input expansion 核心补充：`196 passed`；
- 静态检查：修改文件 `ruff check`、Python compile、`git diff --check` 通过；
- 完整回归：`897 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider；本轮使用 fake OpenAI-compatible 400 和本地 summary response 验证控制流。

### 62.7 学习重点

1. Context overflow 不是普通 API 参数错误；错误分类本身就是 Agent Core 控制流的一部分。
2. `process_result("compact")` 只有被外层真正消费、改变持久 history 并恢复 turn，才算生产能力。
3. compaction retry 与 HTTP retry 完全不同：前者改变请求内容，后者只等待后原样再发；把两者混用会稳定复现失败。
4. 恢复链必须保留失败 step 的耐久证据，同时确保它不会作为空 assistant 污染下一次 Provider 请求。

### 62.8 剩余差距

- InfCode 用统一 `MessageV2.ContextOverflowError` 类型和 Provider error parser；NZ-Coder 跨 OpenAI-compatible Provider 仍需依赖经过测试的窄字符串集合，公网 Provider 互操作尚未验证；
- InfCode 的 compaction process 还有 summary 请求自身 payload overflow 的 strip/recovery 分支；本节完成时 NZ-Coder 尚未实现，该缺口已由 A062 关闭；
- 当前三次 guard 覆盖 Provider reactive overflow；send-time 本地 hard-limit preflight 已直接 compact，但没有与 reactive counter 合并为单一 compaction-attempt owner；
- Question/Suggestion dismiss、provider-executed tool metadata/attachments 仍未进入统一 processor metadata；
- A057 的 PatchPart/Session summary diff 在本节完成时仍未实现，已由 A061 关闭；
- 本轮不代表 Agent Core 完全同构或 SWE-bench 分数与 InfCode 等价。

## 63. A060：Agent Core Question result 与 Provider tool metadata

### 63.1 InfCode 参考能力

本轮核对了以下生产链：

- `packages/opencode/src/tool/question.ts`：Question 正常回答返回 `title + output + metadata.answers`；dismiss 不是 error，而是 `title="Question dismissed"`、`output="User dismissed the question."`、`metadata={answers: [], dismissed: true}`；
- `packages/opencode/src/session/processor.ts`：`tool-result` 把 output title/metadata/attachments 写入 completed ToolPart；Question 的 `dismissed=true` 是一种正常用户回答并继续，其他 dismissed tool 才按配置 blocked；`tool-input-start/tool-call` 还把 `providerExecuted` 与 `providerMetadata` 写到 ToolPart 顶层；
- `packages/opencode/src/session/prompt.ts`：普通/MCP tool execute wrapper 返回结构化 ToolOutput；工具可通过 `ctx.metadata()` 在 running 阶段更新 title/metadata；MCP image/blob 可成为 ToolPart attachments；
- `packages/opencode/src/question/index.ts`：Question request/reply/reject 有独立 deferred 生命周期；GUI client 还会创建 QuestionPart 和 QuestionSummaryPart。

### 63.2 NZ-Coder 原有不足

NZ-Coder 的 Question dismiss 文本已经是普通成功结果，因此 A058 后不会 blocked，行为方向正确；但 handler 只能返回裸字符串，answers/dismissed/title 进入 ToolExecutor 后全部丢失，Session ToolPart 无法区分“用户回答”“用户 dismiss”和普通文本结果。与此同时 OpenAI Responses 与 Gemini adapter 已在 `tool_call.provider_extra` 真实产生 response item/reasoning replay metadata 或 thought signature，但 SessionProcessor 的 pending/running ToolPart 没有保存，Session snapshot/HTTP consumer 看不到这些 Provider 事实。

### 63.3 实现结果

当前链路为：

```text
Question handler
  -> ToolOutput(str subclass, title, metadata)
  -> ToolExecutor keeps visible str + structured result facts
  -> SessionProcessor.settle_tool(... title, metadata)
  -> completed ToolPart.state.{title, metadata}

Provider stream tool delta/provider_extra
  -> accumulated normalized tool call
  -> pending/running ToolPart.metadata
  -> complete-call reconciliation preserves metadata
  -> message_records / Session save-load / HTTP projection
```

具体变化：

- 新增 `ToolOutput(str)`：handler 仍满足项目统一字符串返回合同，直接 `dispatch()` 的旧调用者仍得到可比较/可打印的字符串；只有 ToolExecutor 额外读取 title/metadata；
- Question answer 输出与 InfCode 对齐为 `Asked N question(s)`、`metadata.answers` 和继续提示；
- Question dismiss 输出与 InfCode 对齐为 `Question dismissed`、`User dismissed the question.`、`metadata={answers: [], dismissed: true}`；它保持 completed，不设置 permission denial/blocked，并进入下一次模型请求；
- `ToolExecutionResult` 增加 title/metadata，普通工具默认空值，不改变既有构造器和调度分类；
- `SessionProcessor` 在 completed state 保存 result metadata，并在 ToolPart 顶层保存 Provider tool-call metadata；流式 provisional call ID 按 index 对账时不会丢失已经到达的 metadata；
- message schema 的 ToolPart validator/round-trip 同时保留顶层 Provider metadata 与 completed state result metadata，不再在 checkpoint 时清空；
- OpenAI Responses 的 response item/reasoning replay metadata、Gemini thought signature 已有真实 producer，本轮不修改 Provider wire contract，只补 durable Session consumer。

### 63.4 关键设计决策

1. **保持 handler 的 str 合同**：没有把所有工具接口改成新 dataclass；`ToolOutput` 是 str-compatible 的内部增强，旧 CLI、测试、dynamic tool 和 SWE runner 不需要迁移。
2. **dismissed 不是 denied**：Question dismiss 表示用户不回答，模型应按最佳判断继续；不能复用 Permission rejection 的 blocked 状态。
3. **Provider metadata 与 result metadata 分层**：前者描述模型/tool-call 协议，保存在 ToolPart 顶层；后者来自工具执行结果，保存在 completed state，和 InfCode MessageV2 层次一致。
4. **只接真实 producer**：NZ 没有 Provider-executed tool，也没有 Suggestion tool，因此不写 `providerExecuted=false` 或伪造 Suggestion dismissed；空字段不构成对齐。
5. **attachment 不做半闭环**：MCP 当前把非文本 content 放入受信边界内的字符串，尚无 ToolPart attachment→下一次 Provider multimodal message 的完整 consumer，本轮不只加 UI 字段冒充完成。

### 63.5 关键文件

- `nz_coder/tools/__init__.py`：str-compatible `ToolOutput` 与 dispatch 保留；
- `nz_coder/tools/question.py`：Question answer/dismiss 的真实 title/metadata producer；
- `nz_coder/runtime/tool_executor.py`：结构化 result facts 分类与传递；
- `nz_coder/runtime/session_processor.py`：Provider metadata 与 completed result metadata 的 durable owner；
- `nz_coder/runtime/loop.py`：stream delta metadata 与 tool result consumer 接线；
- `nz_coder/message_schema.py`：ToolPart metadata validation/round-trip；
- `tests/test_question.py`、`tests/test_terminal_interactions.py`：字符串兼容、answer/dismiss metadata；
- `tests/test_session_processor.py`、`tests/test_session_events.py`、`tests/test_loop_fake.py`：Provider delta、Session projection 和 dismiss-continue 闭环。

### 63.6 验证结果

- Question/terminal/Session/message/Loop/permissions/observability/MCP 聚焦：`157 passed`；
- 静态检查：相关文件 `ruff check`、Python compile 与 `git diff --check` 通过；
- 完整回归：`899 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider；Provider metadata 使用既有 Responses/Gemini 本地 fixture 验证。

### 63.7 学习重点

1. Question dismiss 和 Permission reject 都来自用户交互，但 Agent 控制语义相反：前者是成功回答并继续，后者默认 stop。
2. 工具显示文本不能承担全部控制事实；title/answers/dismissed 必须作为结构化字段耐久保存。
3. Provider metadata 在 adapter 中出现并不等于 Agent Core 已消费；必须穿过 stream accumulator、processor、checkpoint 和 Session projection。
4. 兼容旧字符串 handler 时，str subclass 可以作为窄桥接，但不能借此隐藏任意新协议；新增字段仍需真实 producer 和 consumer。

### 63.8 剩余差距

- InfCode GUI client 为 Question 创建独立 pending/completed/terminated QuestionPart 和 QuestionSummaryPart；NZ-Coder 的 Terminal/HTTP pending interaction 仍由 interaction service/event 表达，durable message 中只有 ToolPart result metadata；
- InfCode 工具可在 running 阶段调用 `ctx.metadata()` 更新标题/进度；NZ handler API 仍只有最终字符串/ToolOutput，没有通用增量 metadata callback；
- NZ 没有 Suggestion tool，因此没有 Suggestion dismissed producer/control path；
- NZ Provider adapter 当前没有 provider-executed tool 事件，因此未实现 `providerExecuted`；
- MCP image/blob/resource attachment 尚未进入 ToolPart attachment、Session projection和下一轮 Provider multimodal context 的完整闭环；
- A057 的 PatchPart/Session summary diff 在本节完成时仍未实现，已由 A061 关闭；
- 本轮不代表 Agent Core 完全同构或 SWE-bench 分数等价。

## 64. A061：Agent Core snapshot PatchPart 与 Session summary diff

### 64.1 InfCode 参考能力

本轮继续沿 A057 的 snapshot 主链阅读真实 producer、storage 和 consumer：

- `packages/opencode/src/snapshot/index.ts`：`patch(hash)` 比较 step 起点与当前 workspace；`diffFull(from,to)` 生成 file、patch、additions、deletions、status，并把单文件 patch 限制在 256 KiB；
- `packages/opencode/src/session/message-v2.ts`：`PatchPart` 保存起点 snapshot hash 和变化文件清单；user message 的 `summary.diffs` 只保存轻量文件统计；
- `packages/opencode/src/session/processor.ts`：step finish 后调用 snapshot patch；只有文件真实变化时才附加 `PatchPart`；
- `packages/opencode/src/session/summary.ts`：从最早 step-start 到最新 step-finish 重算 Session 净 diff，聚合 additions/deletions/files；完整 patch 与轻量 message summary 分层保存；
- `packages/opencode/src/session/index.ts` 与 server route：Session diff 是可独立读取的正式 consumer，HTTP 路径为 `GET /session/:id/diff`。

### 64.2 NZ-Coder 原有不足

A057 已经有真实 step start/finish snapshot 和安全 message revert，但 snapshot 只服务恢复。每个 step 修改了哪些文件没有 `PatchPart`；timeline 仍看不到 turn 级增删统计；Session 没有从 snapshot 重算的净 diff；HTTP snapshot、磁盘 Session 和 context compaction 也无法消费这些事实。ChangeTracker 可以显示当前修改和风险，但它按工具注册追踪，不应成为跨 step Session diff 的第二真值。

### 64.3 实现结果

当前生产链为：

```text
StepStartPart.snapshot
  -> tools settle / transaction commit
  -> StepFinishPart.snapshot
  -> WorkspaceSnapshotStore.diff_full(start, finish)
     -> changed files exist: PatchPart(hash=start, files)
     -> current real user: lightweight summary.diffs
     -> whole Session: bounded full diffs + additions/deletions/files
        -> Session JSON + session_diff.json
        -> compaction marker carry-forward
        -> terminal timeline Changes column
        -> idle HTTP snapshot.summary
        -> GET /session/:id/diff
```

具体变化：

- `WorkspaceSnapshotStore.diff_full()` 从已校验的 manifest/blob 生成 added/deleted/modified diff；文本用统一 diff，二进制或非 UTF-8 文件保留状态但 patch 为空；单文件 patch 上限与 InfCode 一致为 256 KiB；
- 工具 step 完成并取得 finish snapshot 后，只有真实变化才生成 `PatchPart`，hash 指向该 step 的起点 snapshot，文件清单稳定排序；只读 step 不制造空 part；
- 当前真实 user turn 从该 turn 的第一个 step-start 到最新 finish 计算净 diff，并投影到 user message `info.summary.diffs`；不把完整 patch 重复写进每个 user message；
- Session 从历史最早 step-start 到最新 finish 重算净结果；完整 patch 总量再限制为 2 MiB，超预算文件仍保留路径、状态和增删统计；
- 自动/overflow compaction 会把最新 Session summary marker 复制到 summary message，旧头部移出 active history 后仍可提供 Session diff；
- `save_session()` 把聚合 summary 写入 Session JSON，把完整有界 diff 原子写入 Session runtime artifact；terminal timeline 直接显示文件数和 `+A/-D`；
- loopback HTTP 增加 `GET /session/:id/diff` 与标准库 client `diff()`，idle snapshot 增加 summary；两者都从 durable message marker 投影，不另维护 HTTP 状态。

### 64.4 关键设计决策

1. **snapshot 是 diff 真值**：Session/turn diff 只比较 step snapshots；ChangeTracker 继续负责事务风险和旧 `/undo` fallback，但不参与这些统计。
2. **PatchPart 只存索引事实**：与 InfCode 一样保存起点 hash 和文件清单；完整 patch 单独放在 Session summary/artifact，避免每 step 重复大文本。
3. **净差异而非累计相加**：多 step 对同一文件反复修改甚至恢复时，从最早 start 到最新 finish 重算，结果不会重复计数。
4. **有界且可降级**：snapshot 本身已有文件/总量边界；diff 再限制单文件和 Session 累计 patch。二进制、非法 UTF-8 或过大 patch 不阻断 Agent，仍保留可审计的文件状态。
5. **不复制 Git backend**：InfCode 使用私有 Git index；NZ-Coder 延续 A057 的标准库 SHA-256 manifest/blob store，以满足无 Git workspace 和不新增依赖合同。复制的是生命周期和数据语义，不是 TypeScript/Git 实现细节。

### 64.5 关键文件

- `nz_coder/runtime/workspace_snapshot.py`：manifest 间完整有界 diff；
- `nz_coder/runtime/loop.py`：step PatchPart、turn summary 和 Session summary producer；
- `nz_coder/runtime/session_processor.py`：durable PatchPart owner；
- `nz_coder/message_schema.py`：PatchPart、message summary、Session summary/diff projection；
- `nz_coder/state/context.py`：compaction 后保留最新 Session diff marker；
- `nz_coder/state/sessions.py`：Session summary 与 `session_diff.json` artifact；
- `nz_coder/interface/timeline.py`：turn 级 Changes consumer；
- `nz_coder/http_service/{manager,server,client}.py`：idle summary 与 Session diff HTTP consumer；
- `tests/test_workspace_snapshot.py`、`tests/test_session_processor.py`、`tests/test_message_schema.py`、`tests/test_loop_fake.py`、`tests/test_timeline.py`、`tests/test_http_service.py`：producer→storage→consumer 闭环。

### 64.6 验证结果

- 聚焦回归：workspace snapshot、processor、message/context/Session、timeline、Loop、HTTP 共 `174 passed`；
- 静态检查：相关文件 `ruff check`、全包 compile 和 `git diff --check` 通过；
- 完整回归：`903 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` deprecation warning；
- 未运行 SWE-bench，未调用付费 Provider；本轮是本地 Agent Core/Session 数据链，不需要公网互操作。

### 64.7 学习重点

1. 有 start/finish snapshot 仍不等于 patch 能力；必须生成 durable PatchPart，并有 timeline、Session artifact 或 API consumer。
2. step 文件清单、user turn 轻量 diff 和 Session 完整 diff 是三个不同粒度，不能把同一大 patch 复制到每层。
3. Session summary 应从边界状态重算净结果，而不是累加每个 step 的 additions/deletions。
4. 压缩会删除旧 active history，因此会话级事实必须显式迁移到 compaction message，否则长会话的 diff 会悄悄消失。

### 64.8 剩余差距

- InfCode 私有 Git diff 可表达 rename/copy 等更丰富状态；当前 manifest 比较把 rename 表达为 delete + add；
- 二进制/non-UTF8 文件只保留 changed status，additions/deletions 为 0，patch 为空；
- 当前 user turn 关联基于 NZ-Coder 线性 OpenAI-style history；没有 InfCode parentID 子树/分支 Session 的同构查询；
- 任意历史 part 的图形化 diff 浏览、diff event push 和 IDE consumer 尚未实现；当前真实消费者是 timeline、Session artifact 和 HTTP pull；
- snapshot capture 被慢仓库上限跳过时，该 step 不会产生 PatchPart/summary，系统不会伪造不完整 diff；
- 本轮不代表完整 Agent Core 同构，也不证明 SWE-bench 分数与 InfCode 等价。

## 65. A062：Compaction 摘要请求 payload/context overflow 恢复

### 65.1 InfCode 参考能力

本轮阅读的不是普通主请求 overflow，而是 compaction 模型调用本身失败后的完整路径：

- `packages/opencode/src/kilocode/session/compaction-payload-recovery.ts`：识别 `ContextOverflowError`、`request entity too large` 和 `function_payload_too_large`；先 strip completed tool output/media，再持久降级 input expansion；重建 payload 并比较 JSON bytes，只有变小才重试一次；
- `packages/opencode/src/session/compaction.ts`：单次恢复后仍返回 compact 时，区分单个 oversized natural user turn、aggregate head 和没有 tail boundary 三种情况；前两者写占位 summary 并继续，最后一种写可见 ContextOverflowError；
- `packages/opencode/src/infcode/session/input-expansion.ts`：`compaction-failed` 只降级 synthetic expansion，不碰 user natural language；
- `packages/opencode/src/kilocode/session/prompt.ts` 与 `session/prompt.ts`：同一 user turn 的 compaction attempt guard 上限为 3；主请求 compact outcome 进入 compaction task，summary 内部 payload retry 不是无界外层循环；
- `packages/opencode/test/kilocode/compaction-payload-recovery.test.ts`、`test/infcode/session/overflow-expansion-recovery.test.ts`：验证错误匹配、tool/media strip、缩小后重试以及 natural/synthetic user text 边界。

### 65.2 NZ-Coder 原有不足

A059 已能把普通 Provider 主请求的 context overflow 转成 compact outcome，并最多恢复三次；但 `auto_compact()` 自己只发一次摘要请求。若该请求因模型窗口或网关 payload 上限失败，异常会直接结束 run。旧工具输出和 `_nz_input_expansions` 即使可安全裁剪也不会被利用；反过来，若没有任何可裁内容，简单增加 retry 又会浪费一次必然失败的付费调用。单个超长自然粘贴或 aggregate head 也没有 InfCode 的安全占位边界。

### 65.3 实现结果

当前链路为：

```text
main request asks for compaction / manual compact
  -> archive original transcript first
  -> build bounded head + preserved recent tail
  -> summary request
     -> ordinary error: propagate unchanged
     -> payload/context overflow:
        -> compact every head tool result persistently
        -> tombstone only tagged input expansions
        -> rebuild summary messages
        -> afterBytes < beforeBytes ? retry exactly once : skip retry
        -> success: anchored summary + tail
        -> overflow again / no shrink:
           -> oversized natural user turn: paste placeholder
           -> aggregate head + real tail: history placeholder
           -> no safe boundary: preserve visible exception
  -> compaction marker + trace recovery facts
```

具体变化：

- 新增窄错误分类，只响应 InfCode 对应的 context overflow、`request entity too large` 和 `function_payload_too_large`，rate limit、auth、普通 400/5xx 等错误不进入破坏性降级；
- recovery 对 summary head 中的 tool result 写入既有 compact marker 和时间，不只修改临时 request；`compact_stored(..., "compaction-failed")` 同样持久化 expansion tombstone；
- 使用重新序列化的 conversation JSON byte 数比较前后 payload；没有缩小就不执行第二次 LLM 调用；
- 缩小后只允许一次内部 retry，并在 prompt 中说明旧工具输出/媒体已移除；第二次同类 overflow 不再循环；
- 单个 user-authored natural text 超过 usable input 时，写 InfCode 同语义的 oversized-paste placeholder；tagged expansion 使用 `_nz_user_text` 分离，因此不会把 synthetic 文件正文误判为自然粘贴；
- 没有单一 oversized turn、但存在近期 tail boundary 时，用 aggregate-history placeholder 丢弃旧 head、保留最近完整回合；没有 tail 时拒绝清空全部历史并重新抛出错误；
- 原始 transcript 在任何 strip 之前写入唯一 archive；compaction marker 记录 before/after bytes、裁剪数量、是否 retry 和 fallback reason，Agent trace 投影 `context_compaction_payload_recovery`。

### 65.4 关键设计决策

1. **先缩小再重试**：retry 是昂贵且可能有副作用的 Provider 调用；payload 未变时不靠次数掩盖确定性失败。
2. **降级必须持久化**：只改第二次临时 request 会让下一 turn 再次携带相同 payload；tool compact time 和 expansion tombstone 必须回写 Session history。
3. **natural user text 是硬边界**：正常 strip 不修改用户原话。只有已经确认单个自然 turn 自身超过窗口、无法摘要时，才通过显式占位 summary 丢弃，并保留预降级 archive。
4. **tail 是 aggregate fallback 的授权边界**：有 tail 才能证明仍保留近期上下文；无 tail 时不能为了“继续”静默清空整个 Session。
5. **翻译 processor 语义而非伪造 AI SDK**：InfCode 从 SessionProcessor 的 `compact/stop` outcome 得知 summary 失败；NZ-Coder 的 provider helper直接抛异常，因此在 `auto_compact()` 内实现同一一次性恢复状态机。

### 65.5 关键文件

- `nz_coder/state/context.py`：payload error 分类、持久 strip、byte shrink guard、单次 retry 和两类 placeholder fallback；
- `nz_coder/runtime/loop.py`：Agent-bound provider/capability snapshot 继续用于 recovery request，并写 recovery trace；
- `nz_coder/state/input_expansion.py`：复用既有 `compaction-failed` 持久 tombstone producer；
- `tests/test_context_budget.py`：工具输出缩小、tagged expansion、无缩小跳过、二次 overflow、oversized user turn、aggregate tail 和无安全边界错误测试。

### 65.6 验证结果

- 新增 6 项 summary self-overflow 定向测试；context budget 文件共 `20 passed`；
- Context/InputExpansion/Session/HTTP/Loop/CLI/Hook 聚焦回归 `187 passed`；
- 相关文件 `ruff check`、全包 compile 和 `git diff --check` 通过；
- 第一次全量回归出现 1 个无关 BackgroundAgent cancellation 状态读取竞态，隔离测试及其 9 项文件复跑均通过；随后完整回归稳定为 `909 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未发公网/付费 Provider 请求。

### 65.7 学习重点

1. 主请求能够触发 compaction，不代表 compaction 自己不会 overflow；summary model call 是独立失败边界。
2. “最多重试一次”仍不够，必须先证明请求真的缩小，否则只是确定性重复付费。
3. payload recovery 与普通 micro-compact 不同：失败恢复会裁掉 head 中所有已完成 tool output，以尽快越过硬上限。
4. 安全降级需要 archive、持久 marker、tail boundary 和明确 placeholder 四者共同成立。

### 65.8 剩余差距

- NZ-Coder 当前没有进入 Provider history 的原生 multimodal FilePart，因此只实现真实存在的 tool output 与 tagged expansion strip；没有伪造 media attachment producer；
- InfCode recovery 状态由 durable assistant summary message/error/finish 表达；NZ-Coder 使用同步 summary helper、compaction marker 和异常表达，数据模型不是同构；
- payload byte guard 只计算 summary conversation JSON，与 InfCode一样不含 recovery prompt；真实 provider SDK envelope 可能另有少量开销；
- InfCode 同一 turn 的 usage-triggered overflow 与 reactive compact 共用 `compactionAttempts`；NZ-Coder 的 send-time/last-usage compaction 与 reactive counter 仍是两个 owner，统一 guard 尚未完成；
- placeholder 能继续 Session，但被丢弃 head 的语义只能从原始 transcript archive 人工恢复，不会自动再注入；
- 公网 Provider 对 413/body-limit 的错误对象形态尚未互操作验证；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 66. A063：Agent Core 单一 compaction-attempt owner

### 66.1 InfCode 参考能力

本轮核对了 compaction cap 的 owner、两个 producer 与 exhaustion consumer：

- `packages/opencode/src/session/prompt.ts`：每次 `runLoop(sessionID)` 初始化一个 `compactionAttempts=0`；`lastFinished` usage overflow 和当前 Provider 返回 compact 都先调用同一 guard，再递增并创建 compaction task；
- `packages/opencode/src/kilocode/session/prompt.ts`：`MAX_COMPACTION_ATTEMPTS=3`；达到上限时设置 turn close reason error，并把 `ContextOverflowError`/finish error 附到可用 assistant message；没有 message 也必须关闭 turn；
- `packages/opencode/test/kilocode/session-compaction-cap.test.ts`：四次 top-level overflow 只允许三次 summary；调用序列为 overflow/summary 重复三轮，第七次 top-level overflow 在摘要前触发 guard；同时验证 guard 在有/无 assistant message 时的状态变化；
- manual compaction task 直接进入 `compaction.process()`，不经过上述自动 overflow reserve，因此不是自动恢复额度的 producer。

### 66.2 NZ-Coder 原有不足

A059 的 `compaction_attempts` 只统计 reactive Provider overflow。`_compact_if_needed_async()` 根据上一 assistant usage 或完整 request estimate 触发的自动摘要不进入该计数，因此同一次用户 run 理论上可以先做若干 pre-send summaries，再额外做三次 reactive summaries。两个 owner 与 InfCode 不一致，也无法保证第 4 次自动摘要前停止。

另外，NZ-Coder 的 summary 是 user message 加 preserved tail，而 InfCode 有独立 `summary=true` assistant。压缩后 `_last_assistant_usage_total()` 会重新看到 tail 中压缩前的旧 assistant usage，可能把同一 overflow 证据重复消费，制造假 attempt。

### 66.3 实现结果

当前自动压缩控制流为：

```text
AgentLoop._run starts one _CompactionAttemptState(attempts=0)
  -> pre-send cleanup
     -> no hard overflow: no reserve
     -> usage/request hard overflow: reserve shared attempt before summary
  -> Provider request
     -> reactive context overflow: reserve same shared attempt before summary
  -> reserve 1/2/3: execute compaction
  -> reserve 4: raise exhaustion before summary call
     -> attach durable _nz_error to assistant (or create error step)
     -> checkpoint Session run_status=error
     -> trace + finalize turn error

manual /compact or explicit compact tool
  -> _compact_messages directly
  -> does not consume automatic attempt state
```

具体变化：

- 新增 run-local `_CompactionAttemptState`，唯一持有自动 attempt 数；上限常量为 3；
- pre-send `_compact_if_needed_async()` 和 reactive compact 分支都必须在调用 summary model 前 `reserve()`；trace 中两条路径使用同一递增 attempt 编号；
- 第四次 reserve 抛出内部 exhaustion，禁止摘要调用；错误写到当前/最近 assistant；如果首个请求前尚无 assistant，则创建带 step-start/step-finish error 的 durable assistant message；
- exhaustion checkpoint 保存 `run_status=error`，随后走标准 finalize；重启后不会只有终端瞬时文本而没有 Session 证据；
- pre-send summary 自身出现非 exhaustion 异常也会写 durable error step、checkpoint 并安全结束，不再从 `_run()` 裸抛到 CLI；
- sync `_compact_if_needed()` 同样支持显式 attempt owner 并返回是否发生 compaction，便于非异步 consumer 保持合同；生产 run 使用 async 路径；
- `_last_assistant_usage_total()` 读取最新 compaction marker 的 `created_at`；只消费该时间边界之后的新 assistant usage，preserved tail 中旧 usage 不再重复触发；
- `/compact` 命令和 `manual_compact_hook` 继续直接调用 `_compact_messages()`，不绑定 run-local自动 guard，与 InfCode manual task 边界一致。

### 66.4 关键设计决策

1. **attempt 在摘要前 reserve**：上限是“允许执行几次 compaction”，不是失败后才记账；否则仍会发生第 4 次付费调用。
2. **owner 生命周期是一轮用户 run**：新用户请求重新建立 state；不把 counter 写成模块全局或跨 Session 永久额度。
3. **soft cleanup 不计数**：micro-compact、input expansion preflight degradation 不调用 summary model，因此不占 compaction attempt。
4. **manual 与自动恢复分离**：用户主动 `/compact` 是明确操作，不应因为之前自动恢复三次而被静默拒绝；模型显式 compact tool 同理。
5. **时间边界补偿数据模型差异**：NZ 没有 InfCode summary assistant，不能照搬 `summary !== true` 判断；用 compaction `created_at` 与 assistant `_timestamp` 表达“该 usage 已被哪次 summary 消费”。

### 66.5 关键文件

- `nz_coder/runtime/loop.py`：attempt owner、pre-send/reactive reserve、durable exhaustion、safe pre-send failure 与 usage boundary；
- `tests/test_loop_fake.py`：pre-send/reactive 混合序列、三次 summary 上限和 pre-send summary failure checkpoint；
- `tests/test_context_budget.py`：compaction 前后 assistant usage 时间边界。

### 66.6 验证结果

- 新增 3 项定向测试；实际混合序列只发生 `[pre-send, reactive, pre-send]` 三次摘要，第二次 Provider overflow 在第 4 次摘要前结束；
- Loop/Context/Session/HTTP/CLI/Hook 聚焦回归 `161 passed`；
- 相关文件 `ruff check`、全包 compile 和 `git diff --check` 通过；
- 完整回归 `912 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 66.7 学习重点

1. 两条路径各自限制三次不等于总共限制三次；attempt 必须有唯一 owner。
2. cap 检查发生在副作用之后就失去意义，尤其 summary 是付费 Provider 调用。
3. 数据模型不同会改变控制判断：InfCode 可跳过 `summary=true` assistant，NZ 必须建立等价的 usage consumption boundary。
4. error close reason、durable message error、Session checkpoint 和 trace 是不同消费者，exhaustion 不能只打印一行终端文本。

### 66.8 剩余差距

- NZ 的 `_nz_error` 仍是内部 message 字段，不是 InfCode `MessageV2.ContextOverflowError` 的公开 typed schema；HTTP message projection 不公开内部错误对象；
- compaction boundary 依赖本进程生成的浮点时间；旧 Session 若 assistant 缺少 `_timestamp`，会保守视为已被最新 compaction 消费；
- A062 的 summary 内部一次 payload-shrink retry 不单独占 run-level attempt，这与 InfCode recovery helper一致，但一次 attempt 最多可能包含两次 summary Provider call；
- send-time soft cleanup 与 hard summary 的触发实现仍是 NZ 的同步架构翻译，不是 InfCode queued CompactionPart/assistant-summary 数据模型同构；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 67. A064：Agent Core 运行中工具 metadata 与 Bash progress

### 67.1 InfCode 参考能力

- `packages/opencode/src/tool/tool.ts`：`Tool.Context.metadata({title, metadata})` 是工具执行期间的正式回调，不是最终返回值的别名；
- `packages/opencode/src/session/processor.ts`：metadata 回调更新 `running ToolPart.state` 并发布 part update，最终 tool result 再替换为 completed state；
- `packages/opencode/src/tool/bash.ts`：命令启动前报告 description/workdir/空 output，输出 chunk 到达时更新有界 preview，结束时返回 exit/output/truncated 等最终 metadata；
- `packages/opencode/src/session/message-v2.ts`：running state schema 本身允许 title/metadata，Session 消费者可在工具未结束时观察它们。

### 67.2 NZ-Coder 原有不足

- A060 只有工具结束后的 `ToolOutput.title/metadata`，handler 执行期间没有与 call ID 绑定的回调；
- `SessionProcessor.start_tools()` 只能把 pending 改成空 running，`message_schema` 又会丢弃 running title/metadata；
- Bash 使用阻塞式 `subprocess.run(capture_output=True)`，长命令结束前终端/HTTP 只能看到 started，无法判断是否仍有输出；
- 若直接增加模块级 callback，多个 Agent 或并行工具会串 call，因此不能用全局可变单例补这个字段。

### 67.3 实现结果

核心调用链：

```text
AgentLoop tool batch
  -> scoped_tool_metadata_reporter(execution-local sink)
  -> ToolExecutor scoped_tool_call(provider call id)
  -> bash.report_tool_metadata(title, preview metadata)
  -> AgentLoop callback
  -> SessionProcessor.update_tool_metadata(call id)
  -> running ToolPart + message.part.updated
  -> atomic Session checkpoint
  -> HTTP SSE/replay and Session snapshot consumers
  -> final ToolOutput
  -> completed ToolPart(title/output/exit/workdir/truncated)
```

具体变化：

- 新增两个 `ContextVar` scope：一个绑定当前 Agent 的 metadata sink，一个绑定当前并行 tool call ID；同步线程池显式 `copy_context()`，异步 `to_thread` 也传播该上下文；
- `report_tool_metadata()` 保持 handler 返回 `str` 的既有注册合同，Agent 外直接调用时是安全 no-op，回调异常不改变工具执行结果；
- `SessionProcessor.update_tool_metadata()` 只接受 pending/running part，保留原 input/start time，完成或失败后拒绝晚到更新；processor 和 checkpoint bridge 使用可重入锁串行化并行只读工具的写入；
- message schema 正式保留 running title/metadata，使磁盘 Session、HTTP snapshot 和 SSE replay 不再丢字段；
- Bash 改用 `Popen`、合并 stdout/stderr 的 reader thread 与有界 preview；POSIX timeout 杀整个新 process group，避免 shell 子进程泄漏；最终仍保留 `Error:`/非零命令合同，并以 `ToolOutput(str)` 附带结构化完成信息；
- 输出进度最多每 100ms 持久一次，避免每个极小 chunk 都触发 checkpoint；最终模型可见输出继续使用既有 `CONTEXT_TRUNCATE_CHARS` 上限。

### 67.4 关键设计决策

1. **call ID 与 reporter 都是 ContextVar**：只绑定 reporter 仍无法区分同一批并行工具；两层 scope 才对应 InfCode 每个 execute context 的隔离语义。
2. **Session part 是事实源**：没有新造 `session.tool.progress` 平行状态；HTTP 已消费 `message.part.updated`，重连也能从 checkpoint/replay 恢复。
3. **Bash 合并 stdout/stderr**：避免两个 reader 的到达顺序伪装成原始 shell 顺序，同时与 InfCode 的单 output preview 接口一致。
4. **不在本轮添加 attachments/QuestionPart**：MCP binary attachment 若不进入下一轮 Provider 多模态消息就是半截字段；GUI-style QuestionPart 也必须先统一现有 terminal/HTTP InteractionBroker 的事实 owner。
5. **测试 Harness 降级兼容**：有些取消测试把 `_execute_tools_async` 单独绑定到最小 Harness；不存在 callback factory 时使用 no-op reporter，保持该内部可测试边界。

### 67.5 关键文件

- `nz_coder/tools/__init__.py`：execution-local metadata reporter 与 current call scope；
- `nz_coder/runtime/tool_executor.py`：dispatch 前绑定 provider tool call ID；
- `nz_coder/runtime/loop.py`：progress→processor→checkpoint bridge；
- `nz_coder/runtime/session_processor.py`：running metadata 状态迁移与并发保护；
- `nz_coder/message_schema.py`：running title/metadata 持久投影；
- `nz_coder/tools/bash.py`：增量输出、超时 process-group 清理和最终结构化结果；
- `tests/test_bash_progress.py`、`tests/test_session_processor.py`、`tests/test_session_events.py`：producer、状态机、durable event 集成链。

### 67.6 验证结果

- Bash progress/timeout、processor running projection、Session event 集成与 cancellation Harness 共 18 项通过；
- 修复旧 `subprocess.run` 测试替身后，最终相关聚焦 15 项通过；
- changed files `ruff check`、全包 `compileall`、`git diff --check` 通过；
- 完整回归 `916 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 67.7 学习重点

1. “有 running 状态”不等于支持运行中信息；必须同时有执行期 producer、call correlation、durable state 和实时/恢复 consumer。
2. ContextVar 只解决 scope 传播，不自动解决同一 scope 中并行 call 的身份；call ID 也要独立绑定。
3. 将阻塞命令改成 streaming 不只是换 API，还要处理 stdout/stderr 顺序、timeout 子进程、输出上限和 checkpoint 写放大。
4. SSE event 应是 durable part 的投影；若只发瞬时 progress event，断线恢复后产品状态会倒退。

### 67.8 剩余差距

- 当前只有 Bash 使用运行中 metadata API；Read/Search 很快且没有自然 chunk producer，子 Agent/MCP 是否接入要按真实长任务 consumer 决定；
- 终端 renderer 仍主要消费 tool started/completed 卡片，不实时重绘 running output；HTTP SSE/Session snapshot 已是本轮真实 consumer；
- Bash timeout 会保留此前 running preview 事件，但最终 ToolPart 按现有错误合同不保留 partial output；取消信号仍需等待执行 worker 自行收口，尚无 InfCode `ctx.abort` 等价物；
- MCP image/audio/resource attachments 尚未进入 ToolPart→Session→下一轮 Provider 的完整多模态链；
- Question 仍由 terminal/HTTP InteractionBroker 管理 pending UI，尚未形成 InfCode QuestionPart/QuestionSummaryPart 的 durable 同一事实源；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 68. A065：Agent Core durable QuestionPart 生命周期

### 68.1 InfCode 参考能力

- `packages/opencode/src/tool/question.ts`：question tool 先生成 `QuestionID`，为 GUI client 写 pending QuestionPart，再把同一 ID 交给 Question service；answer 后更新 completed card 并追加 QuestionSummaryPart，dismiss 后更新 terminated；
- `packages/opencode/src/question/index.ts`：pending map 与 deferred 是交互 owner；asked/replied/rejected 事件都携带 request/session/tool correlation，reply/reject 后从 pending map 删除；service finalizer reject 全部未完成请求；
- `packages/opencode/src/session/message-v2.ts`：QuestionPart 支持 pending/completed/terminated/error、request ID、tool call ID、questions/response/error；QuestionSummaryPart 保存问题与答案；
- InfCode 只为 app/desktop/VS Code/JetBrains 创建 display parts，终端 TUI 主要消费 Question service event；part 是 durable display projection，不取代 pending service。

### 68.2 NZ-Coder 原有不足

- A060 只把 answer/dismiss 写进 completed ToolPart metadata，没有独立 QuestionPart/QuestionSummaryPart；
- HTTP `InteractionBroker` 在 `ask_question()` 内自行生成随机 ID，工具和 durable Session 不知道这个 ID，无法把 pending API 请求、SSE 和恢复后的卡片关联起来；
- Session schema 不接受 question part，哪怕临时写入也会在 save/load projection 时丢失；
- 服务崩溃后，HTTP 会把 run 标成 interrupted，但旧 pending/running ToolPart 或 pending question display 若存在，会成为没有 deferred consumer 的幽灵状态。

### 68.3 实现结果

核心调用链：

```text
question tool validates input
  -> generate question-<uuid> request ID
  -> lifecycle pending(call ID, request ID, questions)
  -> SessionProcessor QuestionPart(status=pending)
  -> bind same request ID around question_asker
  -> HTTP InteractionBroker registers/publishes exactly that ID
  -> reply | reject | timeout | cancel
  -> lifecycle completed | terminated | error
  -> completed QuestionPart + QuestionSummaryPart
     or terminated/error QuestionPart
  -> Session checkpoint + message.part.updated/SSE replay
  -> existing ToolOutput completes ToolPart and model continuation
```

具体变化：

- question tool 现在拥有 request ID，并通过 ContextVar 把它交给 terminal/HTTP asker；HTTP broker 只在没有上游 ID 时生成 fallback，且拒绝 pending ID collision；
- 新增 execution-local lifecycle reporter，携带 current tool call ID、request ID、标准化 questions、answers/error；和 A064 一样，不使用跨 Session 的模块级可变 callback；
- `SessionProcessor` 新增 start/complete/terminate/fail question 状态迁移；completed 同时追加稳定 ID 的 QuestionSummaryPart，terminated/error 不制造 summary；
- Question display questions 显式保存 `custom=true`，对应 InfCode GUI 自动允许 custom answer 的显示合同；
- message schema 对 question/question-summary 做数量、选项、answer shape 和字符串长度边界验证，save/load、HTTP snapshot、SSE replay 共用相同 projection；
- answer、dismiss、malformed service response 和 asker exception 都结算 display state；late update 在非 pending 状态被拒绝；
- 新 run 和 HTTP interrupted restore 会把旧 pending/running ToolPart 结算为 interrupted error，把 pending QuestionPart 结算为 terminated，且恢复 snapshot 不暴露虚假 pending interaction。

### 68.4 关键设计决策

1. **request ID 由工具生成**：这与 InfCode source order 一致，避免 UI broker 生成一个 Session 无法关联的第二身份。
2. **broker 仍是等待 owner，part 是 durable projection**：pending map/deferred 必须留在线程安全 interaction service；Session part 负责显示、重连和审计，二者通过同一 request ID 与事件顺序关联。
3. **NZ 对所有带 SessionProcessor 的前台 client 写 part**：InfCode 用 client flag 限定 GUI；NZ 的 CLI 与 HTTP 共用后端 history，没有可靠的 GUI brand flag，因此按真实 Session consumer 而非伪造 client 名称决定。终端仍可继续直接消费 asker，不声称已改成 part-driven renderer。
4. **恢复时终止而非重新挂起**：deferred、终端输入和 HTTP connection 都不能跨进程恢复；重新展示 pending 卡会误导用户。终止旧请求后，新 Agent run 可根据 interrupted tool result 重新评估是否提问。
5. **dismiss 仍是成功 ToolOutput**：QuestionPart terminated 描述 UI 交互结束；对应 ToolPart completed+metadata.dismissed 描述模型收到正常 dismissal 结果，两者语义不冲突。

### 68.5 关键文件

- `nz_coder/tools/question.py`：request ID owner、request scope 和 lifecycle producer；
- `nz_coder/http_service/interactions.py`：消费上游 request ID 的 pending broker；
- `nz_coder/runtime/loop.py`：execution-local lifecycle→processor→checkpoint bridge；
- `nz_coder/runtime/session_processor.py`：QuestionPart/QuestionSummaryPart 状态机；
- `nz_coder/message_schema.py`：question part validation/projection 与 interrupted reconciliation；
- `nz_coder/http_service/manager.py`：崩溃恢复时结算不可恢复的 pending interaction display；
- `tests/test_loop_fake.py`、`tests/test_session_processor.py`、`tests/test_http_service.py`：真实 Loop、schema/event、共享 ID 和 restore 闭环。

### 68.6 验证结果

- answer/dismiss、共享 HTTP request ID、processor projection、cancel/restore 共 38 项定向测试通过；
- HTTP/Session event/message schema/Loop/Question/cancellation 核心聚焦 `151 passed`；
- changed files `ruff check`、全包 `compileall`、`git diff --check` 通过；
- 完整回归 `920 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 68.7 学习重点

1. QuestionPart 不是另一个提问服务；它是 pending service 的 durable display projection，必须共享 request/tool/message identity。
2. answer 与 dismiss 会同时影响两种状态：Question display 的 completed/terminated，以及模型 ToolPart 的 completed result；不能把两者混成一个 status。
3. “持久 pending”不代表可恢复等待。没有可恢复 deferred/connection 时，正确恢复语义是 terminated/interrupted，而不是继续显示可回答状态。
4. request ID 必须在阻塞 asker 之前产生并发布，否则快速 reply/reject 会先于 durable card 建立，形成竞态。

### 68.8 剩余差距

- NZ 尚未实现 InfCode `blocking`、reply `source`、question `title`、i18n key 和 editor-context follow-up；当前工具 schema也没有这些真实 producer；
- terminal renderer 仍通过直接 question asker 展示，不从恢复后的 QuestionPart 重绘历史卡；HTTP/SSE/Session snapshot 是 durable part 的当前主要 consumer；
- HTTP pending broker 是进程内状态，重启只会 terminated，不支持跨进程继续回答同一个请求；这与实际连接生命周期一致，但不等于分布式 durable queue；
- plan approval 继续由独立 plan_exit/PlanModeController 管理，没有伪装成普通 QuestionSummaryPart；
- MCP multimodal attachments 与下一轮 Provider 消费链仍未对齐；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 69. A066：Agent Core 图片 FilePart 与下一轮 Provider replay

### 69.1 InfCode 参考能力

- `packages/opencode/src/tool/read.ts`：Read 识别 JPEG/PNG/GIF/WebP，在读取完整文件前拒绝 `>= 10 MB` 图片，成功时返回 `Image read successfully` 和 data-URL FilePart；
- `packages/opencode/src/mcp/index.ts`：MCP client 的 `convertMcpTool()` 只返回 raw `client.callTool()` 结果，本身不承担附件转换；
- `packages/opencode/src/session/prompt.ts`：真正的 MCP execute wrapper 遍历 raw content，把 image 与 resource blob 转成 FilePart，并补 Session/message/part identity；
- `packages/opencode/src/session/processor.ts`：tool-result 把 attachments 写入 completed ToolPart；
- `packages/opencode/src/session/message-v2.ts`：下一轮建模时按 model vision capability、compacted 状态和 Provider 是否支持 tool-result media 做过滤；支持时随 tool result 发送，不支持时在完整 tool-result 序列之后插入 synthetic user media message。

这次复核也纠正了一个审查错误：只读 `mcp/index.ts` 会误以为 InfCode 没有 MCP attachment 转换；必须继续追到 `session/prompt.ts` 的 execute wrapper，才能找到真实 owner。

### 69.2 NZ-Coder 原有不足

- `read_file` 对图片按 UTF-8 replacement 文本读取，模型看到的是无意义行内容；
- MCP formatter 会把非 text content JSON 序列化进可见输出，image/blob base64 可能作为巨量不可信文本进入上下文；
- `ToolOutput`、`ToolExecutionResult`、completed ToolPart 和 message projection 没有 attachment 数据通路；
- 模型能力只有 tools/stream/reasoning/temperature 等事实，没有 image-input capability；
- 即使临时保存图片，OpenAI Chat/Responses、Anthropic 和 Gemini adapter 也不会构造合法的下一轮多模态请求；
- 没有 MIME/data URL/base64/数量/单图与总量边界，不能安全持久化外部 MCP payload。

### 69.3 实现结果

核心调用链：

```text
read_file image
  or MCP image / image resource blob
  -> validated ToolOutput(str, attachments=[FilePart])
  -> ToolExecutor extracts internal attachments
  -> SessionProcessor completed ToolPart.state.attachments
  -> checkpoint / save / load / message projection
  -> AgentLoop derives attachment by provider tool_call_id
  -> model supports_image_input + result not compacted
  -> Provider-specific request conversion
     OpenAI Chat: all consecutive tool messages, then synthetic user image turn
     Responses: function_call_output items, then input_image user item
     Anthropic: image block inside native tool_result content
     Gemini: functionResponse followed by inlineData in the same user content
```

具体变化：

- 新增共享 attachment validator，只接受最多 4 个 JPEG/PNG/GIF/WebP base64 data URL；单图与累计解码尺寸都必须小于 10 MB，拒绝 remote URL、MIME 不匹配和 malformed base64；
- Read 用文件签名识别四类图片，并在 `read_bytes()` 前读取 stat 拒绝超限文件；成功输出保持字符串兼容，同时带 title/preview/truncated 与一个 FilePart；
- MCP execute binding 消费 image 和 resource blob；支持的图片转为 FilePart，resource text 保留为文本，不支持或非法 blob 只产生 omission marker，不把原始 base64 泄漏成模型文本；
- `ToolOutput`→`ToolExecutionResult`→`SessionProcessor`→message schema 全链保留 attachment；正常持久路径只在 ToolPart 保存一份，tool message 不重复保存 base64；无 processor 的测试/兼容路径才在 tool message保留 fallback；
- `_sanitize_messages()` 从 assistant durable ToolPart 按 call ID 恢复下一轮 attachment；非视觉模型、已 compact 的旧结果和 token 预估路径都会剥离 media；
- models.dev `modalities.input` 与内建 GPT-5/Codex/Claude/Gemini family 规则提供 image capability；精确本地 catalog 仍可覆盖；
- 四种 Provider wire adapter 按各自协议转换，不把 `_nz_attachments` 私有字段直接发给 Provider；OpenAI synthetic media 必须等待连续 tool-result 段全部结束，避免破坏 tool-call/result 配对顺序。

### 69.4 关键设计决策

1. **ToolPart 是唯一 durable owner**：图片 base64 较大，不能同时复制到 assistant part、tool message 和另一个 attachment store。发送前从 call ID 投影，兼顾恢复与去重。
2. **能力过滤发生在 Agent projection**：Provider adapter 只处理已批准的附件；非视觉模型不会收到 image 字段，也不会把 data URL 降级成文字。
3. **Provider 形状不能强行统一**：Anthropic 能把 media 放进 tool_result；OpenAI Chat/Responses 使用独立 user media turn；Gemini 使用 functionResponse/inlineData。统一内部 FilePart，不统一错误的 wire shape。
4. **MCP 外部 payload 默认不可信**：只转换当前四类真实可消费图片；audio、video、PDF 和任意 resource blob 不因为 InfCode schema 可表达就盲目进入当前 Provider。
5. **附件不参与文本 token 启发式**：data URL 不能当普通字符估算，否则会触发虚假的文本 compaction；精确图像计费/视觉 token 估算仍是后续能力。

### 69.5 关键文件

- `nz_coder/attachments.py`：FilePart 构造、data URL/base64/数量/尺寸验证与 OpenAI synthetic media projection；
- `nz_coder/tools/files.py`：图片签名、读取前尺寸守卫和 Read producer；
- `nz_coder/mcp/runtime.py`：MCP raw content→text/FilePart execute wrapper；
- `nz_coder/tools/__init__.py`、`nz_coder/runtime/tool_executor.py`：字符串兼容 ToolOutput 与内部 attachment transport；
- `nz_coder/runtime/session_processor.py`、`nz_coder/message_schema.py`：completed ToolPart durable storage/projection；
- `nz_coder/runtime/loop.py`：call-ID attachment 恢复、vision/compaction filter 与去重；
- `nz_coder/providers/capabilities.py`、`registry.py`：image-input capability；
- `nz_coder/providers/openai_compatible.py`、`openai_responses.py`、`anthropic.py`、`gemini.py`：下一轮 Provider wire replay；
- `tests/test_tool_attachments.py`、`tests/test_mcp.py`：producer、持久化、能力过滤、协议时序和 Provider shape。

### 69.6 验证结果

- 首轮 Read/Session/Provider 聚焦 `83 passed`；补齐 MCP wrapper 后最终相关聚焦 `116 passed`；
- 覆盖读取前超限拒绝、remote/malformed payload、ToolPart save/load projection、非视觉/compacted filter、连续多 tool-result 排序，以及四类 Provider 请求结构；
- `ruff check`、全包 `compileall`、`git diff --check` 通过；
- 完整回归 `931 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未调用付费或公网 Provider/MCP endpoint。

### 69.7 学习重点

1. 源码级对齐必须追“谁生产、谁持久化、谁恢复、谁消费”，不能看到 MCP client 返回 raw result 就停止搜索。
2. 多模态不是把 base64 塞进历史；它需要 capability gate、协议合法排序、compaction 行为和恢复后的身份关联。
3. 大对象状态应有一个 durable owner，其余层做短生命周期投影，否则 Session 很快被重复 payload 膨胀。
4. 外部 MCP content 同时是功能输入和安全边界；格式、大小与 MIME 必须在进入长期历史前验证。

### 69.8 剩余差距

- InfCode FilePart 还能表达 PDF/audio/video 和任意 MCP resource blob；NZ 当前只开放四类图片，因为现有 Provider adapter 没有统一可靠 consumer；
- InfCode Read 支持 PDF/DOCX 转 Markdown、目录 glob metadata 与 binary-file 专门错误；NZ 本轮没有扩展这些 Read 分支；
- InfCode 还可从用户 FilePart、webfetch 等 producer 产生 media；NZ 当前用户 `/attach` 仍是文本 input expansion，不是原生视觉上传；
- 当前只做启发式 image capability 与 models.dev exact flag，没有真实公网 Provider interoperability 证据，也没有图像 token/费用估算；
- MCP formatter 尚未实现 InfCode output truncation artifact/path 语义，超长纯文本仍沿用 NZ 现有大工具输出持久化；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 70. A067：InfCode WebFetch 文本/图片生产链

### 70.1 InfCode 参考能力

- `packages/opencode/src/tool/webfetch.ts`：只接受 HTTP(S)，默认 Markdown，可选 text/html；默认 30 秒、最大 120 秒；按 Content-Length 和实际 body 双重限制 5 MB；HTML 用 Turndown 转 Markdown，text 分支移除 script/style 等不可见内容；
- 同一工具根据 response MIME 区分文本与图片，图片返回 `Image fetched successfully` 和 FilePart attachment；
- execute 前调用 `ctx.ask({permission: "webfetch", patterns: [url]})`，默认 Agent permission 配置在 `agent/agent.ts` 中允许 webfetch，但仍可按配置改成 ask/deny；
- attachment 后续由 A066 已核对的 SessionProcessor→MessageV2→Provider 链消费。

### 70.2 NZ-Coder 原有不足

- 工具注册表没有 webfetch，模型只能尝试 Bash/curl，无法获得稳定 schema、超时、响应大小和图片返回合同；
- Bash/curl 输出不会把图片转换成 ToolPart attachment，视觉模型无法在下一轮消费；
- HTML 原文会把 script/style 噪声带入上下文，也没有 Markdown/text/html 三种显式格式；
- 外部响应没有统一的 redirect scheme、Content-Length、流式 body 与压缩后大小边界；
- permission 规则中没有独立 webfetch identity。

### 70.3 实现结果

核心调用链：

```text
model calls webfetch(url, format, timeout)
  -> PermissionChecker webfetch rule/default read-only allow
  -> normalize absolute HTTP(S) URL + IDNA hostname
  -> bounded urllib GET + HTTP(S)-only redirect
  -> Content-Length <= 5 MB
  -> read at most 5 MB + 1
  -> bounded gzip/deflate decode <= 5 MB
  -> image MIME -> ToolOutput attachment -> A066 durable replay
     HTML -> Markdown/Text standard-library renderer
     other text/HTML format -> ToolOutput text
```

具体变化：

- 新增 `webfetch` read tool，并在 Agent Loop 的副作用 import 中注册；schema 与 InfCode 一致包含 required URL、format enum 和可选 timeout；
- URL 只允许 HTTP/HTTPS，拒绝 credential、无 host、非法端口和控制字符；Unicode host 转成 IDNA ASCII，permission/tool trace 看到的 URL 不依赖视觉相似字符；
- 默认 30 秒，调用者可指定正数且最高截断到 120 秒；Accept header 随 markdown/text/html 变化；
- Content-Length、无长度流式读取和 gzip/deflate 解压后三层都执行 5 MB 上限，避免压缩炸弹绕过 body 限制；
- redirect 继续经过 HTTP(S) URL validator，不能跳到 `file://`；loopback 明确绕过环境代理，远程 URL 仍保留系统代理行为；
- JPEG/PNG/GIF/WebP response 直接复用 A066 validator 和 ToolOutput attachment，下一轮 Session/Provider 逻辑不创建第二条媒体链；
- 用标准库 HTMLParser 实现当前需要的 heading、paragraph、list、link、code/pre、换行及不可见标签过滤；HTML format 按源码合同保留原文；
- webfetch 默认在 default/plan/acceptEdits/auto 中作为 read-only allow，同时在 deny/allow/ask settings rules 中保留可配置身份。

### 70.4 关键设计决策

1. **复用 A066，而不是在 webfetch 内调用 Provider**：工具只生产 ToolOutput；Session 和 Provider projection 仍是唯一附件消费链。
2. **标准库翻译 Turndown**：项目约束禁止新增依赖，因此实现有界 HTMLParser 子集；不宣称与 Turndown 每个边缘标签逐字符一致。
3. **三层大小检查**：只信 Content-Length 会被缺失或伪造 header 绕过，只限制压缩 body 会被 gzip bomb 绕过。
4. **loopback 直连**：本机环境代理曾把 `127.0.0.1` 转发并返回 502；本地开发服务必须确定性直连，远程地址仍可使用用户代理。
5. **permission identity 独立**：虽然默认 Agent 策略允许 read-only webfetch，用户仍可通过现有 settings 将它设为 ask/deny，不能把它匿名归入 generic read 后失去控制。

### 70.5 关键文件

- `nz_coder/tools/webfetch.py`：URL、HTTP、redirect、timeout、压缩/大小边界、HTML 转换与图片 producer；
- `nz_coder/runtime/loop.py`：生产注册入口；
- `nz_coder/tool_platform/permissioning/checker.py`、`tool_groups.py`：webfetch 默认与可配置 permission；
- `tests/test_webfetch.py`：本地 HTTP fixture 覆盖格式、图片、redirect、gzip 和三类超限；
- `tests/test_permissions.py`：默认 allow 与 ask rule。

### 70.6 验证结果

- webfetch/permission/attachment/MCP/Provider/Smoke 聚焦 `122 passed`；
- 本地真实 HTTP server 覆盖 HTML Markdown/Text/HTML、图片 redirect、gzip、404、非法 scheme/credential/format/timeout、Content-Length 超限、无长度 body 超限和解压后超限；
- `ruff check`、全包 `compileall`、`git diff --check` 通过；
- 完整回归 `937 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未访问公网 URL，也未调用付费 Provider。

### 70.7 学习重点

1. Web fetch 是 Agent 工具合同，不等于让模型执行 curl；响应边界、格式转换、permission 和多模态结果都必须稳定。
2. 网络 payload 的大小必须在声明值、读取值和解压值三个阶段验证。
3. 新 producer 应复用已有 durable consumer；复制一套 web 图片 Provider 逻辑会产生第二事实源。
4. 测试环境中的代理不是小问题：若 loopback 行为不明确，本地 MCP/Web/Provider fixture 与真实开发服务都会出现不可复现故障。

### 70.8 剩余差距

- InfCode 使用完整 Turndown/HTMLRewriter；NZ 标准库 renderer 未覆盖复杂嵌套表格、嵌套有序列表、图片 alt/src、CSS 可见性和所有 malformed HTML 差异；
- InfCode 对 Cloudflare challenge 403 会用诚实 `kilo` User-Agent 重试一次；NZ 尚未复制该特定响应 header 分支；
- 当前支持 gzip/deflate，不支持 br；公网 TLS、代理、Cloudflare 和真实站点兼容性没有互操作证据；
- image producer 仍限 A066 的 JPEG/PNG/GIF/WebP，InfCode `isImageAttachment()` 接受更多非 SVG image MIME；
- 用户直接提交的原生图片 FilePart 仍未对齐，`/attach` 继续是安全文本 input expansion；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 71. A068：用户原生图片 FilePart

### 71.1 InfCode 参考能力

- `packages/opencode/src/session/prompt.ts` 的 `createUserMessage/resolveUserPart`：用户 file part 解析 workspace 文件；普通文本/目录走 Read 文本，图片读取为 base64 data URL FilePart，并保留 filename/source/message/session identity；
- `packages/opencode/src/session/message-v2.ts`：FilePart 是正式 Session part，包含 mime、url、可选 filename/source；构造模型消息时，视觉模型把 user FilePart 放进原 user turn；
- `packages/opencode/src/infcode/session/message-v2.ts`：非视觉模型过滤 user image FilePart，并把后续 image-describe assistant text 合并回源 user turn；summary/compact 的 `stripMedia` 路径只留附件占位；
- `packages/opencode/src/session/prompt.ts` 2119 行附近与 `infcode/vision/describe.ts`：非视觉模型会在主请求前运行独立 image describe，并用 source user message ID 持久关联描述结果。

### 71.2 NZ-Coder 原有不足

- A048/A054 的 `/attach` 和 inline `@file` 对所有文件一律建立 text input-expansion；图片会用 UTF-8 replacement 解码，产生二进制乱码；
- A066 虽然完成 Read/MCP tool-result 图片链，但 user message 没有 FilePart schema/持久 owner；用户必须等待模型调用 `read_file` 才可能看到图片；
- Session projection 不接受 user `type=file` part，临时字段会在 save/load normalization 时消失；
- Provider adapter 只消费 tool-result attachment，无法把 user 图片与用户文字放在同一 turn；
- 连续 user diagnostic 合并只合并文本，若直接加媒体字段会静默丢失后一条消息的图片。

### 71.3 实现结果

核心调用链：

```text
terminal /attach image or inline @image
  -> existing workspace/symlink/regular-file validation
  -> shared JPEG/PNG/GIF/WebP signature sniff
  -> text file: unchanged input-expansion path
     image: small bounded note expansion + durable user FilePart
  -> message identity + Session save/load projection
  -> AgentLoop reads FilePart from the owning user message
  -> supports_image_input?
       no: keep textual [Attached image: path] note, strip media
       yes: attach media to the same user turn
  -> OpenAI Chat/Responses | Anthropic | Gemini wire conversion
```

具体变化：

- 将四类图片签名识别提升到共享 attachment 模块，Read 和用户输入使用同一 MIME 判定，不依赖扩展名；
- `tag_file_attachments()` 现在真正分流：文本文件继续产生 unresolved `kind=file` expansion；图片产生已解析的轻量 `kind=image` note 和 data-URL FilePart，不再读取成 replacement 文本；
- 沿用 A066 最多 4 张、单张及累计解码大小小于 10 MB 的约束；第 5 张、累计超限或单图 `>=10 MB` 只写明确 omission note，不把 payload 写入 Session；
- message schema 新增 image-only FilePart validation、稳定 part ID、文件名清理、remote URL/base64/MIME/大小复验；`message_records()`、Session JSON 和恢复 normalization 共用该路径；
- 正常 user 图片只在 `_nz_parts` 保存一份；发送前从 owner message ID 投影 `_nz_user_attachments`，不在 message 顶层长期复制 base64；
- `_sanitize_messages()` 按 vision capability 和 `include_attachments` 过滤；连续 user 合并会合并两边 attachment，超过边界时保留独立 user turn而不是丢媒体；
- OpenAI Chat 把 text/image_url 放在同一个 user content；Responses 转成 input_text/input_image；Anthropic 使用同一 user message 的 text/image blocks；Gemini 使用 text/inlineData parts；
- 无图片的普通 CLI user message 不提前注入内部 identity，保持原接口和取消测试的精确消息形状兼容。

### 71.4 关键设计决策

1. **图片与文本附件分流而非替换 `/attach`**：源码、日志、配置等文本文件继续享受 A054 的预算/截断/可恢复展开；只有真实图片签名进入 FilePart。
2. **FilePart 是 user message 的 durable part**：与 ToolPart attachment 的 owner 不同，但两者共享验证器和 Provider media shape；顶层 `_nz_user_attachments` 只是发送期投影。
3. **同 turn 投影**：用户图片不是 tool-result，不能像 OpenAI tool media 那样插入后续 synthetic user turn；必须与原用户问题同时提交。
4. **非视觉降级不夸大**：NZ 当前只提供 `[Attached image: path]`，没有声称模型理解内容；InfCode 的独立 image-describe preflight 仍是下一项差距。
5. **签名优先于扩展名**：避免把改名二进制当文本，也避免仅凭 `.png` 把任意 payload 持久进多模态上下文。

### 71.5 关键文件

- `nz_coder/attachments.py`：共享图片签名、FilePart 验证及 OpenAI same-turn media projection；
- `nz_coder/state/input_expansion.py`：文本/image 分流、note、数量/大小降级；
- `nz_coder/interface/cli.py`：终端提交传入 workspace/session identity；
- `nz_coder/message_schema.py`：durable FilePart 创建、验证和 Session projection；
- `nz_coder/runtime/loop.py`：owner message→发送期 attachment、capability filter 与连续 user 合并；
- `nz_coder/providers/openai_compatible.py`、`openai_responses.py`、`anthropic.py`、`gemini.py`：same-turn wire consumer；
- `tests/test_user_attachments.py`：分流、边界、Session、capability、合并与四 Provider shape。

### 71.6 验证结果

- user FilePart/input-expansion/message schema/Provider/terminal 聚焦 `97 passed`；
- 覆盖混合图片+文本、第 5 张、10 MB sparse image、非法 remote persisted part、Session projection、视觉/非视觉 filter、连续 user 媒体合并与四 Provider same-turn shape；
- `ruff check`、全包 `compileall`、`git diff --check` 通过；
- 完整回归 `942 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未运行 SWE-bench，未调用付费 Provider。

### 71.7 学习重点

1. “终端能选文件”不等于原生多模态；必须区分文本 expansion 与 FilePart，并追到 Provider 的同一 user turn。
2. user FilePart 和 tool attachment 可以共享 payload contract，但 durable owner 与协议排序不同。
3. Session schema 是功能边界：不进入正式 part 的媒体在恢复、HTTP snapshot 或 compaction 后都不可信。
4. 非视觉 fallback 需要真实描述 producer；路径占位只保证不丢事实，不能代替图片理解。

### 71.8 剩余差距

- NZ 尚无 InfCode `describeUserTurnImages`：非视觉模型只能知道文件路径，不能消费缓存/流式的图片描述；
- HTTP Session API 仍只接收 prompt string，没有 multipart/data-URL FilePart 提交与相应大小/认证边界；
- terminal renderer 显示既有附件卡和文件列表，但没有图片缩略图或历史 FilePart 专属重绘；
- 用户 FilePart 当前只允许 workspace 内文件入口和四类图片，不支持拖入 remote URL、PDF/audio/video；
- 图片 token/费用仍未计入模型预算，公网 Provider 多模态互操作仍未验证；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 72. A069：非视觉模型 image-describe preflight

### 72.1 InfCode 参考能力

- `packages/opencode/src/infcode/session/prompt.ts` 的 `describeUserTurnImages()`：在主模型首 step 前检查当前 user FilePart；视觉模型跳过，非视觉模型创建当前 assistant 所属的 running TextPart，并按 source user ID 查找 terminal part 实现幂等；
- `packages/opencode/src/infcode/vision/describe.ts`、`types.ts`、`cache.ts`：逐图描述、进度发布、单图失败隔离、取消结算、图片 MIME/10 MB 边界与缓存；
- `packages/opencode/src/infcode/session/message-v2.ts`：非视觉请求过滤原图片，把 completed image-describe text 回填源 user turn；视觉请求保留原图片并过滤描述；
- `packages/opencode/src/infcode/hint/xml.ts`：把成功和失败项投影为稳定的 `<image_describe filename="...">` hint。

### 72.2 NZ-Coder 原有不足

- A068 已能把图片持久化为 user FilePart，并让视觉模型在同一 user turn 接收媒体；非视觉模型只收到 `[Attached image: path]`，没有任何真实像素理解；
- 没有独立描述 producer、逐项状态、取消终态、source identity 关联和恢复幂等；
- 即使未来临时取得描述，也没有 MessageV2 等价层把它放回图片所属 user turn，容易形成错误 role 顺序或重复描述。

### 72.3 实现结果

- 主链路变为：当前 user FilePart → 创建本 step assistant/StepStartPart → `_prepare_user_image_descriptions()` → 独立 `ProviderImageDescriber` → 同一 TextPart running/terminal 更新 → `_sanitize_messages()` 回填源 user turn → 主模型请求；
- 描述模型由 `NZ_IMAGE_DESCRIBE_PROVIDER`、`NZ_IMAGE_DESCRIBE_MODEL` 配置，可选独立 API key/base URL，并复用现有 OpenAI-compatible/Responses/Anthropic/Gemini adapter；请求不携带工具、非流式且有独立输出上限；
- 图片按顺序处理，一张失败不会终止其他图片；每项保留 source FilePart ID、filename、MIME、status、text/error；批次取消把未完成项写为 error，并把 part 终态写为 interrupted 后继续传播取消；
- completed part 是 Session 内的描述缓存：相同 source user ID 再次进入 loop 不重复调用；崩溃遗留 running/interrupted part 会先删除，再由新 assistant step 重建；
- 非视觉主模型只收到 terminal XML 描述，不收到图片 data URL；视觉主模型完全跳过 preflight，继续收到原始媒体；未配置描述模型时逐项形成明确失败 hint，不伪造“已经看图”；
- TextPart schema 增加有界 `metadata.image_describe`，同时修正 `attach_text_part()`：按 part ID 替换并保留同 owner 的 image-describe TextPart，仍维持主回复 TextPart 在前的既有 Session 顺序；
- 模型能力表补齐 `gpt-4o`、`gpt-4.1`、`gpt-4-turbo` 与 `qwen-vl` 视觉识别，避免示例描述模型被错误拒绝。

### 72.4 关键设计决策

1. **复制生命周期，不复制 InfCode 私有服务**：InfCode 调用 Acode `/acode/v1/image/describe`；NZ 没有该服务，因此用现有 Provider contract 加独立视觉模型配置，保留相同 owner/state/filter 语义。
2. **描述属于当前 assistant step**：这样 preflight、主回复、取消和 checkpoint 共用一个生命周期；描述不会伪装成普通 assistant 对话内容。
3. **Session part 是缓存事实源**：恢复和 HTTP snapshot 已能持久化 parts，不再建立第二套仅内存 cache；source message/part ID 决定是否复用。
4. **失败可见且局部化**：没有配置、模型能力错误、单图 Provider 错误都转成该图片的 error hint；只有取消继续中断整个 run。
5. **当前 user turn 边界**：只检查最新真实 user message，不能在后续纯文本 turn 中回头处理旧图片。

### 72.5 关键文件

- `nz_coder/vision.py`：独立视觉 Provider 调用、逐图状态机、取消和 XML hint；
- `nz_coder/runtime/loop.py`：preflight 插入点、source/owner 幂等、checkpoint/event 及非视觉回填；
- `nz_coder/message_schema.py`：image-describe metadata 验证与多 TextPart 保留；
- `nz_coder/providers/capabilities.py`：常见视觉模型 capability；
- `nz_coder/config.py`、`.env.example`：独立描述模型连接和预算配置；
- `tests/test_image_describe.py`：逐项失败、取消、幂等、视觉跳过、Provider 请求与 Session projection。

### 72.6 验证结果

- image-describe/message schema/user attachment/Loop/模型能力聚焦 `102 passed`；
- `ruff check`、全包 `compileall`、本轮文件 `git diff --check` 通过；
- 第一次完整回归发现 TextPart 顺序兼容问题，修复为主回复前插且保留描述 part 后，最终完整回归 `949 passed`；
- 仍有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 72.7 学习重点

1. 非视觉图片降级不是“把 base64 塞进 prompt”，而是主请求前的一次独立多模态推理，再把文本证据关联回原 user turn。
2. 幂等不能只靠进程内 cache；恢复后仍可判断的 source message/part identity 才是 Session 级缓存键。
3. preflight 也属于 Agent step，必须和主请求一样处理取消、错误、事件与 durable checkpoint。
4. 同一种 TextPart 可以有不同语义；metadata 和 owner 决定它是主回复还是内部描述，不能用“删除全部 text part”的更新策略。

### 72.8 剩余差距

- InfCode 还会对非视觉模型的 Read tool 图片执行 `describeReadToolResult()`；NZ 当前 A066 tool image 在非视觉模型上只过滤媒体，尚未生成文字描述；
- NZ 使用 Session terminal part 作描述缓存，没有 InfCode `vision/cache.ts` 的独立文件缓存、图片内容 hash 去重或跨 Session 复用；
- GIF 虽可作为 FilePart，但部分视觉 Provider/InfCode describe endpoint 支持矩阵不同，尚无公网互操作证据；
- 图片 token/费用未计入上下文预算，HTTP multipart/data-URL 用户入口、缩略图和 PDF/audio/video 仍未实现；
- 本轮不证明公网 Provider、完整 Agent Core 或 SWE-bench 分数对齐。

## 73. A070：非视觉模型 Read 图片描述

### 73.1 InfCode 参考能力

- `packages/opencode/src/infcode/session/prompt.ts` 的 `describeReadToolResult()`：所有工具附件先补 attachment PartID/Session/message owner，但只有工具名为 `read` 时进入图片描述；
- 同文件的 `describeReadToolImages()`：视觉模型或无图片附件时原样返回；非视觉模型调用 `describeImages()`，成功后把 XML hints 追加到 Read output，并把完整结果放入 completed tool state 的 `metadata.imageDescribe`；
- 描述中断时不改 Read output、metadata 或 attachments，让 Read 工具本身保持成功，上游 abort signal 决定停止 turn；
- `vision/describe.ts` 继续提供逐图失败隔离，普通描述错误不会把 Read 工具升级成 tool error。

### 73.2 NZ-Coder 原有不足

- A066 已能把 Read 图片放入 completed ToolPart，视觉模型也能在下一请求收到媒体；非视觉模型会过滤附件，只剩 `Image read successfully`，无法理解图像内容；
- A069 的 user-turn preflight 只处理用户 FilePart，不能覆盖 Agent 自己调用 Read 取得的截图；
- 工具完成链没有描述插入点，也没有与 tool call/attachment 对应的稳定 source identity 和 `imageDescribe` metadata。

### 73.3 实现结果

- 异步生产链变为：并行工具 dispatch 收口 → `_describe_read_tool_results_async()` → `_consume_dispatched_tools()` → completed ToolPart/tool message → 下一主模型请求；
- 仅成功的 `read_file` 图片结果触发描述；`webfetch`、MCP 和其他带附件工具按 InfCode 源码保持原行为；
- 每个附件根据 provider tool call ID 和序号生成稳定 PartID-shaped source ID，描述 XML 追加到原 `Image read successfully` 后；
- ToolExecutionResult metadata 增加 InfCode 风格 `{imageDescribe: {tag: image_describe, data: ...}}`，随后由现有 SessionProcessor 持久到 completed ToolPart；原 attachments 不删除，仍供 Session UI/HTTP 和视觉模型使用；
- 视觉模型跳过描述并继续接收媒体；非视觉模型不接收媒体，但普通 tool message 已含 XML 描述；
- 单图/Provider/未配置描述模型失败成为 completed batch 内的 error hint，不改变 Read 的 dispatch status；
- 描述取消时先保留并持久化未经修改的 Read output/attachments，再传播 `CancelledError`，因此已完成的文件读取不会被错误结算为 interrupted tool error；
- 同步私有兼容路径在没有运行中 event loop 时复用相同 coroutine；生产 Agent loop 使用异步路径，避免嵌套 `asyncio.run`。

### 73.4 关键设计决策

1. **严格限定 Read**：InfCode 明确 `if (tool !== "read") return output`；即使 NZ 的 WebFetch/MCP 也能返回图片，本轮不擅自把产品行为扩展到它们。
2. **描述是 ToolResult 后处理，不是新 assistant part**：user image 描述需要关联回源 user turn；Read 描述本来就属于 tool output，直接追加能保持 provider tool-call/result 顺序。
3. **附件继续保留**：非视觉发送层负责过滤，不能为了文本 fallback 删除 Session 的原始图片证据，否则切换视觉模型或历史重放会丢信息。
4. **Read 成功与描述失败分层**：图片读取成功是工具事实；附加视觉服务失败只能成为描述 item error，不能变成 `dispatch_failed`。
5. **翻译附件 identity**：InfCode 的附件本身是 FilePart；NZ A066 把附件嵌在 ToolPart state，因此用 call ID+index 生成确定性 PartID-shaped source identity，而不重复建立另一份 FilePart 状态。

### 73.5 关键文件

- `nz_coder/runtime/loop.py`：Read result preflight、稳定 attachment source ID、取消后持久化顺序和同步兼容边界；
- `nz_coder/vision.py`：复用 A069 的逐图状态机、Provider 描述器和 XML renderer；
- `nz_coder/runtime/session_processor.py`：既有 completed ToolPart output/metadata/attachments consumer；
- `tests/test_read_image_describe.py`：成功、失败、视觉跳过、非 Read 跳过、直接取消、完整异步 pipeline 和取消落盘。

### 73.6 验证结果

- Read image describe/tool attachment/user describe/cancellation/Loop/Session schema 聚焦 `99 passed`；
- `ruff check`、全包 `compileall`、本轮文件 `git diff --check` 通过；
- 完整回归 `955 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 73.7 学习重点

1. 同样是图片，user FilePart 与 tool-result attachment 的正确描述落点不同：前者回填源 user turn，后者追加原 tool output。
2. 附加服务的失败不能污染主工具语义；Read 成功和视觉描述失败必须是两个层次的状态。
3. 取消正确性依赖结算顺序：先保存已经发生的 Read 事实，再停止 Agent turn。
4. 源码级对齐也包括“哪些对象不处理”；把所有带图工具都描述并不等于更准确地复制 InfCode。

### 73.8 剩余差距

- NZ 仍没有 InfCode `vision/cache.ts` 的文件缓存/content-hash 去重，跨 Session 读取同一图片会再次描述；
- NZ 的描述 Provider 当前返回整段文本，没有 InfCode SSE chunk→running text 的增量显示；Read 源码路径本身也未发布逐 chunk ToolPart metadata；
- WebFetch/MCP 图片对非视觉模型仍只保留工具文本，这是与 InfCode 当前源码一致的边界，但仍是产品能力缺口；
- 图片 token/费用、GIF/不同 Provider 兼容性和公网真实请求仍无证据；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 74. A071：PDF/DOCX document-read preflight

### 74.1 InfCode 参考能力

- `packages/opencode/src/session/prompt.ts`：文件解析阶段把 PDF/DOCX 保留为 FilePart，同时生成 ignored synthetic queue note，不把 base64 二进制直接送入模型；
- `packages/opencode/src/infcode/session/prompt.ts` 的 `convertUserTurnDocuments()`：在 user image preflight 后、主模型前逐文档转换；running/terminal TextPart 属于当前 assistant，按 source user/FilePart identity 幂等，取消保留已完成文档并结算其余项；
- `infcode/document/convert.ts`：10 MB 边界、DOCX 结构验证、PDF 首 20 页 clamp、每次 2000 行、continuation hint、path+mtime sidecar cache，并调用私有 `/acode/v1/embed/convert_to_md` 服务；
- `infcode/document/cache.ts`、`utils.ts`：Session 文件缓存、文件名/路径边界、sidecar 命名与 stale prune；
- `infcode/session/message-v2.ts`：completed/error document_read TextPart 回填源 user turn，原 PDF/DOCX FilePart 和 assistant synthetic part 不直接进入 Provider 消息。

### 74.2 NZ-Coder 原有不足

- A068–A070 只允许四类图片 FilePart；PDF/DOCX 会走普通文本附件 expansion，把 ZIP/PDF 二进制用 replacement character 解码后塞入上下文；
- 没有 document MIME/大小/路径 schema、转换 producer、sidecar、逐项状态或 MessageV2 等价回填；
- 即便系统安装了 PDF 工具，Agent loop 也没有主请求前的 document owner、恢复幂等与取消结算点。

### 74.3 实现结果

- `/attach` 和 `@file` 现在按 PDF signature/扩展名与 `.docx` 扩展识别文档；文档不再作为文本 expansion 读取，而是生成 resolved queue note 和 workspace-relative durable FilePart；
- FilePart 保存 MIME、basename、相对路径、size 和 mtime_ns；schema 限制路径不可绝对/回退/反斜杠、单文件小于 10 MB、最多四份文档，并与图片 FilePart 保持混合顺序；
- 主链路变为：user document FilePart → 当前 assistant/StepStartPart → image preflight → `_prepare_user_documents()` → document converter → terminal document_read TextPart → `_sanitize_messages()` 去掉 queue note并回填源 user turn → 主模型；
- DOCX 用标准库 `zipfile`+ElementTree 提取 paragraph/tab/break，验证 `[Content_Types].xml` 和 `word/document.xml`，限制 entry 数、展开总量、XML 大小并拒绝 DOCTYPE；
- PDF 使用可选系统 `pdftotext`，`pdfinfo` 可用时记录页数；自动读取前 20 页，子进程无 shell、120 秒超时、4 MB 转换输出上限；缺少转换器时生成明确 document error；
- 每份转换最多注入 2000 行和 200,000 字符，长文档追加 continuation；PDF 超过 20 页追加部分读取提示；
- sidecar 位于当前 Session document cache，按绝对源路径+mtime_ns+size hash 命名；同一输入恢复/重试直接复用，不再次解包或启动 PDF 转换；
- 文档读取验证 attachment 后源文件 size/mtime 未变化；变化后要求重新 attach，避免持久 FilePart 指向不同内容；
- running/completed/error/interrupted metadata 进入有界 TextPart schema；completed batch（含单项 error）可复用，stale running/interrupted 会重建；
- converter worker 使用 cooperative cancel event；PDF 子进程会 terminate/kill，Agent 等 worker 收口后再写 interrupted part，不留下取消后的迟到 cache 写入。

### 74.4 关键设计决策

1. **复制协议，替换私有服务**：InfCode 的核心转换器依赖 Acode 私有 HTTP API 和 `pdf-lib`；NZ 遵守无新增 Python 依赖约束，用标准库 DOCX reader 和可选系统 Poppler 翻译，但保留相同 FilePart→TextPart→source user 生命周期。
2. **文档 FilePart不存 base64**：10 MB 文档持久化为 data URL 会放大会话文件和 HTTP snapshot；终端产品已有 workspace owner，因此保存受限相对路径与内容指纹。
3. **queue note 只用于本地状态**：转换完成/失败后，Provider user turn 移除“queued”占位，只保留自然用户文本、其他 expansion 和 `<document_read>` 终态。
4. **源文件变化拒绝静默复用**：NZ 没有先复制所有终端附件到 Session upload cache；size+mtime 校验是避免路径内容漂移的必要补偿。
5. **取消必须收口外部进程**：只给 asyncio task 标 cancelled 不足以停止 `pdftotext`；worker event、进程终止和 await-settle 是一个边界。

### 74.5 关键文件

- `nz_coder/documents.py`：DOCX/PDF 检测、转换、安全边界、sidecar、分页/行/字符限制、XML hint 和 cooperative cancellation；
- `nz_coder/attachments.py`：document FilePart contract 与 mixed user files；
- `nz_coder/state/input_expansion.py`：terminal attachment 的 image/document/text 分流与 queue note；
- `nz_coder/message_schema.py`：document FilePart 和 document_read TextPart metadata 持久化验证；
- `nz_coder/runtime/loop.py`：preflight owner、逐项状态、幂等/中断和 source-user reinjection；
- `tests/test_document_preflight.py`：入口分流、DOCX/PDF、cache、源变化、worker cancel、幂等、视觉独立性、Session projection 与完整 Agent request。

### 74.6 验证结果

- document/image/input-expansion/message-schema/Loop/cancellation/context 聚焦 `111 passed`；
- `ruff check`、全包 `compileall`、本轮文件 `git diff --check` 通过；
- 真实标准库 DOCX extraction+sidecar reuse 通过；本机 `/usr/bin/pdftotext` 对生成的单页 PDF 真实转换通过；
- 完整回归 `966 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 74.7 学习重点

1. 支持文档附件不等于 schema 接受 PDF MIME；必须保证二进制不进 prompt、转换结果有 durable owner、恢复不重复转换。
2. 私有 SaaS conversion endpoint 不能“直接抄”；正确翻译需要明确本地 producer 的能力和失败证据。
3. 路径型 FilePart 必须绑定内容指纹，否则恢复时同一路径可能已经是另一份文档。
4. 取消外部转换的完成条件是进程和 worker 都停止，不是 UI 已显示 interrupted。

### 74.8 剩余差距

- InfCode Read tool 本身支持 PDF `pages`、offset、limit 和 continuation；NZ 本轮只完成 user-turn preflight，`read_file` 的文档分页接口仍未对齐；
- NZ 终端文档引用原 workspace 文件而非复制到 Session upload cache；文件被移动/修改后必须重新 attach，HTTP 上传入口仍不存在；
- DOCX 本地提取不保留复杂表格、图片、脚注、批注、公式和精细 Markdown；PDF 依赖可选 Poppler，扫描件/OCR 不支持；
- sidecar 通过新 hash 避免 stale reuse，但尚未主动清理同路径旧 mtime cache；
- PDF page count 缺少 `pdfinfo` 时只能保守读取前 20 页，无法给出总页数；
- 本轮不证明公网转换服务、完整 Agent Core 或 SWE-bench 分数对齐。

## 75. A072：Read tool PDF/DOCX pagination

### 75.1 InfCode 参考能力

- `packages/opencode/src/tool/read.ts`：`read` schema 同时暴露 `offset`、`limit`、PDF `pages`；在普通 binary 拒绝前识别 PDF/DOCX，调用 `DocumentConvert.read()`，以 `<document_read>` 包装结果并保存 preview/truncated metadata；
- `packages/opencode/src/infcode/document/convert.ts`：转换前校验 10 MB 和 MIME，PDF 无 pages 且超过 20 页时要求显式分页，user-turn preflight 才允许首 20 页 clamp；转换后再按 1-based offset/limit 切 Markdown，并生成统一 continuation；
- `packages/opencode/src/infcode/document/pdf-pages.ts`：接受单页或闭区间，每次最多 20 页，页范围不能超过文档总页数；不同页范围使用不同 sidecar，转换临时文件最终清理；
- `packages/opencode/src/infcode/document/cache.ts`、`utils.ts`：sidecar 绑定 path、mtime 和 page range，并清理同一来源/范围的旧 revision。

### 75.2 NZ-Coder 原有不足

- A071 只在 user-turn attachment preflight 调用转换器；Agent 主动调用 `read_file("x.pdf")` 时仍会进入 `read_text(errors="replace")`，把 PDF/ZIP 二进制伪装成文本；
- `read_file` schema 没有 `pages`，无法读取长 PDF 的后续页；`offset/limit` 只作用于普通文本；
- A071 sidecar key 没有独立 page-range 维度，若直接增加分页，会让首 20 页、指定页范围和全 DOCX 共享错误 cache。

### 75.3 实现结果

- `read_file` 现在在 UTF-8 解码前检测 PDF/DOCX，图片仍优先走 A066/A070 attachment 链，普通文件行为保持不变；
- 新增 `parse_document_pages()`，精确接受 `"5"` 或 `"1-10"`，页码从 1 开始，闭区间最多 20 页；空值、0、逆序、超 20 页和非法格式返回明确错误；
- PDF 未指定 pages 且 `pdfinfo` 确认超过 20 页时不再静默截断，返回带 `pages="1-20"` 的模型可执行指导；user-turn A071 仍使用首 20 页 clamp，两种 consumer 语义分开；
- 显式页范围传给 `pdftotext -f/-l`，无 shell；已知总页数时拒绝越界范围；DOCX 与 InfCode 一致地忽略 pages；
- 文档先转换为 Markdown/text，再应用 1-based `offset/limit`；越界 offset 返回 document error，存在后续内容时追加准确的下一 offset；
- Tool 返回 `<document_read filename path>`，同时以 str-compatible `ToolOutput` 保存 preview、truncated 和 document_read status/error/line/page metadata；转换失败是模型可见 document result，不伪装成 UTF-8 成功；
- sidecar 现按 source-path hash、page-range hash、mtime_ns+size revision 分层；同一 PDF 的不同页范围可并存，只清理同一路径、同一页范围的旧 revision；A071 preflight 与工具读取复用同一个转换事实源。

### 75.4 关键设计决策

1. **复用转换器，不复制工具专用解析器**：attachment preflight 与 Read tool 只有分页策略不同；DOCX 安全验证、PDF 进程边界、缓存和文本上限必须保持单一实现。
2. **长 PDF 的 consumer 语义不同**：用户刚附加文档时首 20 页预览优于整体失败；Agent 显式 Read 时应要求 pages，避免误以为看过整份文件。
3. **页范围是 cache identity 的一部分**：只绑定 path+mtime 会把不同页的 Markdown 当成同一内容；只按页范围清 stale，不能为了更新一页删除其他仍有效页缓存。
4. **转换错误不是 dispatch crash**：与 InfCode 一样，把 bounded failure 放进 `<document_read>`，让模型可以换页、换文件或向用户解释；参数 schema/路径级异常仍遵循工具错误边界。

### 75.5 关键文件

- `nz_coder/documents.py`：page range schema、Read consumer、页范围转换、转换后行分页和 sidecar revision 清理；
- `nz_coder/tools/files.py`：文档分流、`pages` tool schema、XML/metadata ToolOutput；
- `tests/test_document_read_tool.py`：page parser、DOCX line pagination、长 PDF pages-required、越界、page sidecar 隔离和 schema；
- `tests/test_document_preflight.py`：A071 producer 的复用/取消/真实 PDF 回归。

### 75.6 验证结果

- 文档 Read/preflight/图片 attachment 定向 `27 passed`；
- Loop、SessionProcessor、context、input expansion、image describe 组合回归 `146 passed`；
- `ruff check` 与全包 `compileall` 通过；
- 完整回归 `972 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 75.7 学习重点

1. 给 schema 加 `pages` 不等于分页完成；转换命令、总页数判断、越界、行切片、提示和 cache identity 必须一起变化。
2. 同一 converter 可以服务多个 consumer，但 clamp/error policy 应由 consumer 明确传入，不能靠隐式默认猜测。
3. 工具读取二进制文档必须发生在 generic binary/UTF-8 分支前，否则再完善 converter 也没有生产调用链。

### 75.8 剩余差距

- InfCode 用 `pdf-lib` 总能解析页数；NZ 遵守无新增 Python 依赖约束，依赖可选 `pdfinfo`。缺少 `pdfinfo` 时只能让 `pdftotext` 读取请求范围，无法可靠执行“超过 20 页必须 pages”和总页数提示；
- 当前同步工具 handler 的取消由 Agent 的 settled-worker 边界保证不会迟到写状态，但没有把 per-call cancel event 直接注入正在运行的 `pdftotext`；A071 async preflight 已有直接 cooperative cancellation；
- 普通文本 `read_file` 尚未源码级对齐 InfCode 的 2000 行默认、50 KB cap、单行 2000 字符截断、目录分页/展开、missing-path suggestions、binary/UTF 编码判断与 LSP warm；
- DOCX 富格式/OCR、Session upload copy/HTTP document entry 和公网转换互操作仍未完成；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 76. A073：Read tool text/directory core parity

### 76.1 InfCode 参考能力

- `packages/opencode/src/tool/read.ts` 的 `miss()`、`list()`、`readSample()`、`isBinaryFile()`：缺失路径给最多三个同目录候选；目录按 locale 排序并给子目录 `/`；读取 4096-byte sample，以扩展名、NUL/control ratio 和 UTF-16/32 BOM 判定 binary；
- 同文件 `readLines()`：默认 2000 行、1-based offset、单行超过 2000 字符追加固定 truncation suffix，累计 UTF-8 输出超过 50 KiB 时停止；因 limit 截断时继续扫到 EOF以获得精确总行数；
- 同文件 text output：`<path>/<type>/<content>`、逐行 `line: text`、byte-cap/line-limit/EOF 三种互斥尾注，以及 preview/truncated/loaded metadata；成功文本读取后以 scoped background effect 执行 LSP `touchFile()`；
- `packages/opencode/src/kilocode/text-stream.ts`、`encoding.ts`：先严格 UTF-8 stream，失败后用 `chardet` + `iconv-lite` 读取 UTF BOM、legacy Latin/CJK 编码；
- Read directory 分支：对 direct entries 应用 offset/limit并生成 continuation；`includeDirectoryFiles` 是内部 context extra，不属于模型 schema。

### 76.2 NZ-Coder 原有不足

- 普通 `read_file` 一次性 `read_text(errors="replace")`，默认返回全文件；没有 byte/long-line 边界，可能把大文件直接推入 context；
- offset 超范围会被静默 clamp 到 EOF，而不是明确报错；输出是 NZ 私有 `[file: lines]` / `|` 格式，缺少结构化 ToolOutput metadata；
- 仅图片和文档有 binary 分流，ZIP/object/NUL 文件会以 replacement character 文本返回；UTF-16 会因 NUL 被误判或误解码；
- `read_file` 无法读目录、缺失路径没有候选、文本读取不触发 LSP warm。

### 76.3 实现结果

- 新增 `read_support.py`，把 text/directory bounded producer 从写工具逻辑中分离，但仍由唯一 `read_file` 注册入口消费；
- 普通文本默认最多 2000 行；offset 保持 1-based，0 与 InfCode 一样归一为 1，负数/错误类型拒绝；超范围返回文件总行数，不再静默空读；
- 每行先截到 2000 字符并追加 `... (line truncated to 2000 chars)`，然后按 UTF-8 bytes 累计 50 KiB；limit 截断继续扫 EOF，byte cap 截断立即停止并生成下一 offset；
- 输出改为 InfCode shape：absolute `<path>`、`<type>file</type>`、`<content>`、`line: content` 和唯一 continuation/EOF footer；ToolOutput 保存 preview、truncated、loaded 与检测 encoding；
- binary detection 读取 4096-byte sample，覆盖 InfCode 扩展集合、NUL、control-byte ratio；UTF-16/32 BOM 跳过 binary heuristic并用标准库正确解码；图片、PDF/DOCX 分支仍优先于 generic binary；
- 严格 UTF-8失败后，在不新增依赖的约束下尝试 locale、GB18030、Shift-JIS、Big5、CP1252、Latin-1，并以可打印/assigned/CJK score 选择；不再无条件 replacement decode；
- 目录 Read 返回 direct children，locale sort、目录 `/` 后缀、offset/limit、总数/continuation和 preview/truncated metadata；隐藏项与 InfCode一样不自动过滤；
- 缺失路径在父目录按双向 substring 选择最多三个候选；父目录本身不存在时仍稳定返回普通 not-found；
- 成功普通文本读取提交 best-effort LSP warm：同一路径 pending 去重，最多两个 daemon worker，容量满时跳过；worker 结束即退出，不给进程留下常驻 executor。第一次完整回归暴露常驻线程使 fork warning 从 1 增至 2，修正后恢复为 1，副作用已被验证收口。

### 76.4 关键设计决策

1. **byte cap 在 long-line truncation 后计算**：这是 InfCode 的实际顺序；按 Python字符数或原始整行 bytes 都会产生不同 next offset。
2. **limit 与 byte cap 的扫描策略不同**：limit 后继续计数才能给 `of total`；byte cap 必须立即关闭读取，否则 cap 不再保护 I/O。
3. **不伪造 chardet**：标准库 fallback覆盖明确 BOM 和常见 legacy codec，但无法声称与 InfCode `chardet` 对所有编码相同；该差距保留在 76.8。
4. **LSP warm 不阻塞 Read，也不常驻空线程**：bounded daemon worker翻译 InfCode forked effect；重复路径去重，饱和时丢弃 warm而不影响工具成功。
5. **`read_file` 统一读取文件和目录**：保留已有 `list_directory` 兼容工具，但模型现在可以像 InfCode Read一样用同一入口读取 direct directory manifest。

### 76.5 关键文件

- `nz_coder/tools/read_support.py`：bounded line reader、binary/encoding、目录/缺失建议和 LSP warm；
- `nz_coder/tools/files.py`：唯一 Read 分流、XML output、ToolOutput metadata和 2000 行 schema；
- `tests/test_read_file_parity.py`：line/byte cap、offset、目录、suggestion、binary/BOM/legacy、schema和 warm并发边界；
- `tests/test_document_read_tool.py`、`tests/test_tool_attachments.py`：文档/图片分支不回退的交叉回归。

### 76.6 验证结果

- 新增 A073 contract tests `9 passed`；
- Read/Document/Image/Loop/Session/LSP组合回归 `129 passed`；
- `ruff check`、全包 `compileall`、本轮文件 `git diff --check` 通过；
- 完整回归 `981 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 76.7 学习重点

1. “限制 50 KB”必须说明在哪个表示层计算、何时停止和 continuation 指向哪里；否则相同常量并不代表相同行为。
2. binary detection 与 encoding detection有先后依赖；UTF-16/32 BOM是 NUL heuristic 的必要例外。
3. background best-effort 也需要生命周期设计；一个永不 shutdown 的 executor 会改变整个进程的 fork 安全属性。
4. Read 输出是 Agent 搜索循环的协议，不只是终端展示文本；严格 offset 和可执行 continuation 会直接影响定位效率。

### 76.8 剩余差距

- NZ 无新增依赖，因此没有 InfCode `chardet`/`iconv-lite` 的完整 legacy encoding检测与写回保真；当前 codec score 对罕见/短小歧义编码可能选错；
- A073 当时把 Read nested instruction discovery 记为差距；A074 重新阅读当前源码后确认 InfCode 的 `Instruction.find()`/`resolve()` 已明确固定返回空值，nested discovery 当前禁用，因此 NZ 不应新增该行为；
- `includeDirectoryFiles` internal extra、IDE `readFileDetailed`/approximate count、external-directory permission flow 和 ignore/deny directory filtering没有 NZ consumer；
- LSP warm 饱和时是 best-effort drop，且 legacy编码文件进入 NZ LSP client时仍会用 UTF-8 replacement同步；
- 当前路径安全严格限制 workspace 内部；这比 InfCode可授权 external directory更保守，不是 external permission parity；
- 本轮不证明完整 Agent Core 或 SWE-bench 分数对齐。

## 77. A074：Instruction source/budget/injection parity

### 77.1 InfCode 参考能力

- `packages/opencode/src/session/instruction.ts`：当前 `find()` 固定返回 `undefined`、`resolve()` 固定返回 `[]`，注释明确 nested discovery 已禁用；Read metadata 的 `loaded` instruction链当前没有 producer；
- `packages/opencode/src/infcode/session/instruction.ts`：统一收集 global/project rules、CLAUDE.md、AGENTS.md；按 global rule→global Claude→global Agents→project rule→project Claude→project Agents 渲染，读取失败逐源降级；
- `instruction-budget.ts`：单源 20 KiB、总计 32 KiB；先做 per-file UTF-8安全截断，再按 priority 60→10分配总预算，分别记录 per-file truncated、total truncated、total omitted，最终仍按 source order渲染；
- `infcode/rules/index.ts`：project source通过 bounded `git ls-files` 区分“checked into codebase”与“private project instructions”；转义内容中的嵌套 system-reminder标签；
- `infcode/session/prompt.ts`：background rules不并入稳定 system prompt，而是作为独立 leading text注入第一条 user message；没有 user message的 compaction/sub-agent/continue路径回退到 system数组。

### 77.2 NZ-Coder 原有不足

- A073 文档根据旧印象把 nested instruction列为下一项，但当前 InfCode源码实际上明确禁用；照旧候选实现会制造功能偏差；
- NZ已有 20/32 KiB和 source priority，但用一个 `truncated` 布尔混合 per-file、cumulative truncation与 omission，日志和模型提示无法判断具体降级原因；
- project instructions统一标成泛化的“project instructions”，没有区分版本库共享规则与当前 checkout私有规则；
- instruction reminder与 memory/runtime/scratch拼进同一个 dynamic context；虽也进入首 user，但 authoritative rule与 fallible background context没有独立边界，无 user时还会合成 user turn而不是 system fallback；
- system-reminder转义只匹配小写精确标签，大小写或标签内空白可提前闭合 wrapper。

### 77.3 实现结果

- 明确保持 nested discovery禁用，并新增测试保证只发现 workspace root的 `AGENTS.md`/`CLAUDE.md`和 first-level rules；不让后续开发者再次根据旧文档误加 Read-scoped规则；
- instruction预算改为两阶段：先对每个 source做 20 KiB UTF-8 code-point安全前缀，再按 priority分配 32 KiB cumulative预算；
- `InstructionBundle` 增加 `per_file_truncated_count`、`total_truncated_count`、`omitted_count`，旧 `truncated_count` 保持“受影响 source数”而非 flags求和，避免同一 source重复计数；
- 三种终态使用独立模型提示；total omitted仍生成 notice entry，因此模型知道规则被预算省略，而不是静默假装完整；
- project source使用无 shell、2 秒 timeout的 `git ls-files --error-unmatch` best-effort判断；结果按 workspace/path/source mtime缓存。Git缺失、非仓库、timeout或未跟踪均安全降级为 private label，不阻塞 Agent可用性；
- global/project label对齐为“private global for all projects”“checked into codebase”“private project not checked in”；
- reminder wrapper对大小写和标签内空白执行 regex转义，instruction body不能闭合外层 `<system-reminder>`；
- `_build_api_messages()` 不再把 rules拼入 `<context-injection>`。先注入 memory/runtime/scratch，再把 authoritative instruction reminder放在第一条 user内容最前；原始消息无 user时把 reminder追加到 system，不制造伪 user；
- tracer新增三类预算计数，同时保留 source/included/bytes/paths，token projection仍把 instruction预算单独计入。

### 77.4 关键设计决策

1. **以当前源码覆盖旧候选**：InfCode注释和固定空实现是有效源码事实；“Claude Code过去支持 nested”不能作为实现依据。
2. **受影响 source数与降级 flags分开**：一个 source可同时触发 per-file和total截断；兼容总计不能因此把它算成两份文件。
3. **Git只是标签探针**：规则内容无论 Git是否存在都加载；探针失败只改变 shared/private描述，不成为项目运行依赖。
4. **authoritative rules与background context分层**：两者都可注入 user turn，但规则必须保持独立 wrapper并领先，不能被 memory语义“仅供参考”稀释。
5. **无 user fallback不改变对话角色结构**：compaction和continue请求不能仅为承载规则伪造一个 user message。

### 77.5 关键文件

- `nz_coder/state/instructions.py`：source预算状态、UTF-8截断、tracked/private label和 reminder安全渲染；
- `nz_coder/runtime/loop.py`：独立 instruction注入、无 user system fallback和 trace统计；
- `tests/test_instructions.py`：三种预算终态、多字节边界、标签、nested-disabled和 wrapper escaping；
- `tests/test_loop_fake.py`：instruction领先 dynamic context及 no-user system fallback。

### 77.6 验证结果

- 新增 A074 contract tests `7 passed`；
- instruction/Loop/context/compaction/Session/Provider/HTTP组合回归 `180 passed`；
- `ruff check`、全包 `compileall`、本轮文件 `git diff --check` 通过；
- 完整回归 `988 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 77.7 学习重点

1. 源码级对齐必须重新打开当前实现；旧设计、旧文档和上游产品印象都可能已经失效。
2. 上下文预算不仅是裁剪长度，还必须把“为何不完整”作为可观测状态和模型提示保留下来。
3. instruction、memory和runtime state虽然都属于动态上下文，但信任等级不同，不能只因最终都进入 user content就混为一块。
4. 可选 Git集成应提供信息而不是控制可用性；超时/缺失时的降级语义必须明确。

### 77.8 剩余差距

- InfCode有 instruction-file enabled state、create/delete/setEnabled API、SQLite state和 watcher；NZ只有磁盘存在即启用的 runtime loader，没有终端设置或统一Session/API consumer；
- InfCode rules parser验证完整 metadata并对 invalid rule产生 warning；NZ first-level Markdown loader只剥离识别到的简化 frontmatter，缺少结构化 warning/state；
- NZ instruction读取当前同步执行；InfCode以 concurrency 8读取/渲染多个 source。受 32 KiB总预算限制影响较小，但大量损坏/慢文件时仍有差别；
- tracked cache是进程级 path+mtime近似，而 InfCode语义是 Session cache；Git index变化但规则文件不变时，NZ标签可能保持到进程结束；
- global data root和 rules目录使用 NZ品牌路径，不会为了字面一致改成 `.infcode`；
- 当前 nested discovery在两边均禁用，不再列为差距；
- 本轮不证明完整 Agent Core 或 SWE-bench分数对齐。

## 78. A075：Agent Core cooperative tool cancellation

### 78.1 InfCode 参考能力

- `packages/opencode/src/tool/tool.ts`：每个 `Tool.Context` 都持有独立 `abort: AbortSignal`，取消不是 UI 私有状态，而是工具执行合同的一部分；
- `packages/opencode/src/session/prompt.ts`：Session/任务 controller 的 signal 随 tool context下传，任务取消会触发 controller；
- `packages/opencode/src/tool/read.ts` 与 `infcode/document/convert.ts`：Read document把 signal传给转换层，转换中断返回 interrupted，调用方不把半成品当成功结果；
- `packages/opencode/src/tool/bash.ts`：监听 `ctx.abort` 并终止正在运行的命令，metadata标记 aborted；
- `packages/opencode/src/session/processor.ts` 与 `session/message-v2.ts`：中断工具统一落为 `error + interrupted`，丢弃 partial output，并在下一轮生成明确的 interrupted tool result。

### 78.2 NZ-Coder 原有不足

- A046 的 `to_thread_settled()` 只能在 asyncio task取消后等待 Python worker自然结束，不能通知正在运行的工具停止；
- A071 的 user-document preflight有自己的 cancel event，但 A072 直接 `read_file` 的 PDF路径拿不到 per-call signal；慢 `pdfinfo`/`pdftotext` 会继续运行到完成或 timeout；
- Bash timeout能杀进程组，但 Agent Ctrl+C 只取消外层 task，命令本身仍可能运行到自然结束；
- 调度器有 sequential guarded、serial barrier和parallel read三条异步执行路径，若只修其中一条，同一工具会因批次形状不同而表现不同。

### 78.3 实现结果

- 工具注册层新增 execution-local cancellation ContextVar；每个调用使用独立 `threading.Event`，并在 handler执行范围内绑定，避免并行工具和并发 Session串信号；统一 dispatch在 signal已置位时不再启动 handler，封住慢 hook/权限等待后的迟到副作用；
- `to_thread_settled()` 增加 best-effort `cancel_callback`：收到 `CancelledError` 时先发停止信号，再屏蔽重复取消并等待 worker完全收口，最后传播最初的取消；
- Agent Loop的 sequential guarded、scheduled serial barrier、单只读和并行只读四种实际分支全部通过同一个 cancellation wrapper执行；
- `read_file` 将当前 event传入 document converter；PDF页数探测改为可轮询 `Popen`，与 `pdftotext` 一样在取消时终止子进程；写 sidecar前再次检查 event，避免取消后的迟到 cache提交；
- Bash轮询相同 event，取消时复用现有 process-group终止和 stdout reader drain边界；同步 handler虽会内部返回 `Error: Command cancelled`，外层被取消的 Agent调用不会消费该输出；
- SessionProcessor继续作为唯一终态 owner：外层取消只在 worker收口后把未完成工具写成 `error + interrupted`，不保存半截 output。

### 78.4 关键设计决策

1. **线程不能强杀，必须协作取消**：Event负责请求停止，settled bridge负责证明副作用已经停止；二者缺一都会留下迟到写入或长时间假取消。
2. **信号按 tool call隔离**：不能用模块级全局 Event；并行 Read中取消一个调用的上下文不得污染另一个 Session或后续 turn。
3. **取消结果由 Session owner结算**：handler内部的错误字符串只服务直接调用；Agent历史必须使用既有 interrupted ToolPart语义，不能同时保存“command cancelled”完成结果。
4. **先覆盖真实阻塞 producer**：本轮接入 Bash及 PDF外部进程，而不是声称所有 Python工具都能在任意指令点被抢占。

### 78.5 关键文件

- `nz_coder/tools/__init__.py`：per-call cancellation ContextVar与作用域；
- `nz_coder/runtime/async_utils.py`：先 signal、后 settle、再传播取消的线程桥；
- `nz_coder/runtime/loop.py`：所有异步 scheduler分支的 event创建和绑定；
- `nz_coder/tools/bash.py`：消费 signal并终止命令进程组；
- `nz_coder/tools/files.py`、`nz_coder/documents.py`：Read document signal、可取消 `pdfinfo`/`pdftotext` 与 cache提交边界；
- `tests/test_tool_cancellation_context.py`：真实 scheduler/document/进程/Bash取消合同。

### 78.6 验证结果

- cancellation/document/Bash/tool attachment聚焦回归 `38 passed`；
- `ruff check` 与全包 `compileall` 通过；
- POSIX真实子进程 smoke证明 Bash休眠命令在 event后 3 秒内退出；fake `pdfinfo`证明 cooperative cancel调用 terminate；
- 完整回归 `993 passed`，另有 1 条既有 Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网 Provider，未运行 SWE-bench。

### 78.7 学习重点

1. asyncio task取消、Python线程停止、外部进程终止和Session终态是四个不同层次，必须形成一条有序链路。
2. “等待worker结束”只保证安全，不保证响应速度；源码级 `AbortSignal`对齐需要工具实现真正消费信号。
3. 调度器的每条分支都属于工具合同；只在常见并行路径注入 context会产生难以复现的批次依赖 bug。
4. partial progress可以实时展示，但取消终态不能把半截输出重放给模型。

### 78.8 剩余差距

- 当前 cooperative consumers是 Bash和document Read；InfCode的 grep/glob/skill/task/deepmap等也直接消费 `ctx.abort`，NZ对应路径需要逐个核对其阻塞边界后接入；A077随后确认当前InfCode LSP不属于abort consumer；
- Provider HTTP调用仍使用自身的取消/settle逻辑，Python同步网络栈不能被此 tool event强制中断；
- terminal已能取消 Agent turn，但尚未证明 running card在同一帧即时显示 cancelling状态；
- POSIX Bash进程组已做真实 smoke，Windows进程树语义按用户决定不在当前适配范围；
- 本轮不证明完整 Agent Core 或 SWE-bench分数对齐。

## 79. A076：Grep/Glob cooperative abort consumer

### 79.1 InfCode 参考能力

- `packages/opencode/src/tool/grep.ts`：Grep把 `ctx.abort` 作为 `signal` 传入 `Ripgrep.search()`；搜索完成后按文件mtime排序、最多输出100个匹配；
- `packages/opencode/src/tool/glob.ts`：Glob把相同 signal传入 `Ripgrep.files()`，流式取最多101项判断truncated，再读取mtime并排序；
- `packages/opencode/src/file/ripgrep.ts`：`waitForAbort()`把 `AbortSignal` 转为失败 effect，`raceAbort()`与30秒timeout共同包住子进程 exit；stream与search都在 scoped child-process生命周期内运行，abort不是单纯忽略最终结果。

### 79.2 NZ-Coder 原有不足

- `grep_search`通过 `subprocess.run(timeout=30)`执行GNU grep；A075可以取消外层工具线程，但无法通知grep进程提前停止；
- 系统grep缺失时，Python fallback对每个文件执行整文件 `read_text().splitlines()`，既不检查cancel event，也会为大文件制造额外内存峰值；
- `glob_search`调用 `glob.glob(..., recursive=True)`一次性完成遍历后才过滤，扫描期间没有可观察取消点；
- 三条路径在取消时可能继续耗时，随后才由settled worker丢弃结果，安全但响应不等价于InfCode abort。

### 79.3 实现结果

- `_run_grep()`改为无shell `Popen`，stdout/stderr进入临时文件避免pipe回压；以20ms轮询per-call event和30秒deadline；取消或timeout时terminate、等待、必要时kill，worker完全收口后才返回；
- grep完成后解析、mtime排序和格式化循环也检查取消，避免进程恰好结束后仍生成并提交迟到结果；
- Python fallback改为逐文件、逐行流式读取，并在每个文件和每行边界检查event；系统grep缺失分支单独捕获内部中断并返回统一 `Error: Search cancelled`，不让handler抛异常；
- glob改为可迭代的workspace `Path.rglob()`扫描，每个yield检查event，同时保留workspace resolve校验、内部目录排除和相对路径接口；
- `_matches_glob()`补偿 pathlib对零层 `**/` 的差异，确保 `**/*.py`同时命中workspace根文件和嵌套文件；
- Agent路径仍由A075负责把内部cancel字符串丢弃，并由SessionProcessor记录唯一 `error + interrupted` ToolPart。

### 79.4 关键设计决策

1. **终止真实producer而非只丢结果**：外层task取消不等于grep进程已停止；必须等process与worker都结算。
2. **fallback也属于生产合同**：只修系统grep会让同一个工具因机器是否安装命令而拥有不同取消语义。
3. **保留现有NZ工具接口**：本轮只对齐abort consumer，没有把`grep_search`的扩展output modes、相对路径和默认limit强改成InfCode输出。
4. **遍历取消点应靠近I/O**：逐行/逐路径检查比扫描完成后检查更快，也避免把无用完整内容装入内存。

### 79.5 关键文件

- `nz_coder/tools/search.py`：可取消grep进程、流式Python fallback、可取消glob遍历与glob匹配兼容；
- `tests/test_search_cancellation.py`：子进程terminate、glob扫描取消、fallback异常边界与`**/`根路径回归。

### 79.6 验证结果

- search/smoke/cancellation/runtime-context组合回归 `67 passed`；
- `ruff check`、全包 `compileall`和本轮 `git diff --check`通过；
- fake grep process证明event触发terminate并退出worker；受控慢glob generator证明取消后不返回部分列表；
- 完整回归 `998 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网Provider，未运行SWE-bench。

### 79.7 学习重点

1. 一个工具的optional fallback不是测试辅助代码；只要生产环境可能进入，它就必须共享取消、安全和错误返回合同。
2. cooperative cancellation的粒度由I/O循环决定；检查event的位置比检查次数的名义数量更重要。
3. 子进程stdout若使用pipe但父进程只轮询exit，输出填满pipe会死锁；临时文件让等待与取消路径保持独立。
4. 源码级对齐可以只完成一个经过证明的行为切片，但必须明确没有对齐的输出和性能语义。

### 79.8 剩余差距

- InfCode使用bundled Ripgrep 15.1、JSON事件解析、gitignore/hidden规则、partial标记、mtime并发stat与100项行为；NZ仍是GNU grep加Python glob，不声明完整搜索结果parity；
- NZ grep临时输出在30秒内没有字节上限，InfCode以stream处理；需要独立设计输出背压/上限后再对齐；
- `Path.rglob()`只能在yield之间观察event，单个慢目录系统调用本身不能被Python中断；
- `repo_intel.smart_search`、Repo Map扫描、MCP和子Agent仍各有自己的阻塞producer，不能因基础event存在就宣称已取消对齐；A077随后确认当前InfCode LSP没有ctx.abort下传，不能列作此项源码parity；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 80. A077：Task/subagent parent-cancel propagation

### 80.1 InfCode 参考能力与LSP纠偏

- `packages/opencode/src/tool/lsp.ts`：重新核对后确认当前LSP tool没有读取或传递 `ctx.abort`；操作调用LSP service自身的请求/timeout链。因此“下一步给LSP补InfCode abort”不是当前源码事实，本轮不实施；
- `packages/opencode/src/tool/task.ts`：child Session创建后定义 `cancel()`，用 `ctx.abort.addEventListener("abort", cancel)`把父工具取消映射到 `promptOps.cancel(nextSession.id)`；
- 同一Task用 `Effect.acquireUseRelease`注册/移除listener，无论成功、失败或取消都执行release；恢复task沿用原child Session ID；
- child prompt调用继承model、agent、tool restriction；取消的是child Session运行，而不是只让父tool停止等待；
- `packages/opencode/src/session/prompt.ts`：Task注入的 `promptOps.cancel`最终进入Session run-state cancel，使活动child LLM/tool链收到中断。

### 80.2 NZ-Coder 原有不足

- `run_subagent(cancel_event=...)`已有轮次前/Provider后检查、取消状态和事务rollback，但前台注册的 `task` handler默认参数为None，没有读取A075当前tool event；
- `run_subagent_async()`把任务放进settled thread，却没有在async task取消时设置child event，因此可能等待完整child timeout；
- BackgroundAgentManager有自己的event，但child内部dispatch没有把它绑定成当前tool cancellation context；长Bash/PDF/search等nested producer看不到后台取消；
- 活动child Provider同步请求只受总timeout控制，父取消必须等请求返回后才能进入现有cancel检查。

### 80.3 实现结果

- `run_subagent()`把显式event与A075 `current_tool_cancel_event()`合并为一个effective owner；前台task自动继承父call event，后台manager继续使用自己的显式event；
- child worktree执行scope重新绑定同一event，所有nested tool复用A075/A076 consumers和中央dispatch pre-cancel gate，不建立平行全局状态；
- `run_subagent_async()`始终准备effective event，并把 `event.set`交给 `to_thread_settled(cancel_callback=...)`；外层async取消先signal child，再等待run worker结算后传播 `CancelledError`；
- `_completion_with_timeout()`在存在parent event时以50ms轮询Provider future；收到取消后best-effort关闭这个child专属client、cancel future并给1秒settle窗口，然后抛出typed `SubagentCancelled`；
- child loop单独消费typed cancel：general-purpose未提交写先rollback，持久状态写为cancelled，记录run_end/subagent_complete，再返回带Session/worktree身份的cancelled结果；
- 既有Provider timeout、main-thread SIGALRM、普通完成、后台manager cancel和Session resume接口保持不变。

### 80.4 关键设计决策

1. **复用现有child event owner**：NZ已有BackgroundAgentManager event和child状态机；正确做法是接通前台ctx，而不是再增加TaskCancelRegistry。
2. **父工具取消必须进入child内部**：只让父task返回会留下child继续调用模型或修改worktree；nested tools与Provider都必须看到同一信号。
3. **Provider client是child专属资源**：取消时close不会影响父Agent或其他Session client；close失败best-effort降级，typed child终态仍确定。
4. **保留settle边界**：async wrapper不能看到event set就立即遗弃run线程；必须等rollback/state写入完成再向上抛取消。
5. **不伪造LSP对齐**：候选列表不是源码证据；当前InfCode没有LSP abort consumer，就不以产品直觉给NZ加功能后称为parity。

### 80.5 关键文件

- `nz_coder/runtime/subagent.py`：effective cancel owner、async signal/settle、nested scope、Provider polling/close和typed cancelled终态；
- `tests/test_subagent.py`：前台ctx→nested tool、Provider close、async worker settle及持久child cancelled状态。

### 80.6 验证结果

- subagent/AgentManager/Provider/Loop/cancellation/runtime-context组合回归 `110 passed`；
- `ruff check`、全包 `compileall`和本轮 `git diff --check`通过；
- 受控阻塞Provider证明parent event触发child client close；受控async worker证明先signal、settle后抛CancelledError；
- 完整回归 `1001 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网Provider，未运行SWE-bench。

### 80.7 学习重点

1. 模块已经有cancel参数不代表生产链已接通；必须从caller identity追到实际参数producer。
2. 子Agent取消至少有父tool、child run、child Provider、nested tool、transaction和durable state六个参与者。
3. ContextVar适合传播“当前调用”语义，显式event适合BackgroundManager持有；二者可以汇聚成同一对象而不是二选一。
4. 源码级对齐也包括证明某个候选不成立，并把纠偏写进记忆防止后续误实现。

### 80.8 剩余差距

- OpenAI-compatible同步client的 `close()`通常能中断连接，但Python线程不可强杀；若底层adapter忽略close，1秒后Provider helper线程可能继续到自身网络timeout，不过它不再拥有workspace或child状态写入权限；这不等于InfCode原生Session fiber取消强度；
- 当前child Provider请求仍是非流式兼容response，没有InfCode child Session完整stream parts/cost delta生产链；
- child取消状态会持久化，但父Task ToolPart仍由A075 SessionProcessor统一写interrupted，不保存child内部cancel summary；
- 当前InfCode LSP tool没有ctx.abort consumer，因此LSP从“remaining abort consumers”移除；其request timeout/进程恢复应作为独立LSP可靠性主题评估；
- A078随后确认当前InfCode MCP wrapper不消费ctx.abort，并完成skill文件扫描；DeepMap与本地repo-intel仍需按不同产品分别评估；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 81. A078：Skill content/files/abort producer

### 81.1 InfCode 参考能力与候选纠偏

- `packages/opencode/src/mcp/index.ts`的 `convertMcpTool()`：dynamic tool execute只调用 `client.callTool()`，传入progress timeout/reset选项，没有Tool Context或abort signal；因此当前MCP不是 `ctx.abort`源码对齐项；
- `packages/opencode/src/tool/skill.ts`：按name取得Skill并执行permission；filesystem skill返回正文、base directory file URL、相对路径解释与最多10个采样资源文件；
- 同一Skill文件采样调用 `Ripgrep.files({follow:false, hidden:true, signal:ctx.abort})`，排除SKILL.md并在10项处停止；返回title和 `{name, dir}` metadata；
- builtin skill没有filesystem directory，直接返回正文；DeepMap builtin被拒绝并要求使用独立deepmap tool；
- `packages/opencode/src/tool/deepmap.ts`：DeepMap是配置linked remote repositories、启动专用脚本、消费结构化事件并支持远程ask cancel的独立知识产品；NZ本地smart_search/repo_map不能仅因“都搜索代码”就视为对应实现。

### 81.2 NZ-Coder 原有不足

- `load_skill`只返回 `<skill>`包裹的SKILL.md正文；模型不知道skill的base directory，正文引用 `scripts/`、`references/`时无法稳定解析；
- 同目录脚本、模板和参考文件没有样本，模型必须猜路径或额外glob；
- 返回普通str，没有A064 ToolOutput title/metadata，Session工具卡只能使用generic摘要；
- Skill正文lazy read和目录枚举均不检查A075 per-call event；大skill目录取消后仍会继续扫描；
- 旧候选把MCP、DeepMap和repo-intel都写成“remaining abort consumers”，混淆了无abort的MCP wrapper和没有本地同构产品的DeepMap。

### 81.3 实现结果

- Skill正文仍按需lazy读取，但读取前后检查当前tool event；取消不会把半截正文缓存进Skill对象；
- 新增bounded `sample_files(limit=10)`：在skill目录递归枚举，逐路径检查event，排除所有SKILL.md和非文件，并在10项立即停止；
- 资源路径使用skill目录内的词法absolute path，不resolve符号链接目标，避免提示外部真实目标路径；
- `SkillLoader.load()`输出对齐 `<skill_content name source>`、`# Skill`、正文、base directory file URI、相对路径说明、sample note与 `<skill_files>`；
- 保留NZ已有 `allowed_tools`注释、project→user→bundled优先级、conditional activation和unknown-skill Error合同；
- 成功返回str-compatible ToolOutput，title为 `Loaded skill: NAME`，metadata保存name与directory；取消返回 `Error: Skill loading cancelled`，Agent路径由A075持久为interrupted ToolPart。

### 81.4 关键设计决策

1. **路径信息属于Skill可执行语义**：只给正文会让其中的相对脚本引用失去锚点；base URI和资源样本是同一producer的一部分。
2. **采样而非完整清单**：与InfCode一样最多10项，避免大skill目录膨胀工具输出和上下文。
3. **不解析symlink target**：模型需要的是skill目录中的可引用路径，不需要目录外canonical target。
4. **保留NZ扩展metadata**：allowed_tools与三级来源是现有安全/优先级合同，源码对齐不要求删除已有有效约束。
5. **纠偏也是交付物**：MCP无ctx.abort、DeepMap无本地同构consumer必须写明，防止后续按名称硬抄。

### 81.5 关键文件

- `nz_coder/state/skills.py`：cancel检查、资源采样、结构化skill output和ToolOutput metadata；
- `tests/test_skill_loading.py`：正文/base/files/metadata、10项上限、受控扫描取消及unknown error合同。

### 81.6 验证结果

- Skill/Extension/Smoke/RuntimeContext/Loop/Subagent/CLI组合回归 `163 passed`；
- `ruff check`、全包 `compileall`和本轮 `git diff --check`通过；
- 受控慢rglob证明cancel后worker退出且不返回partial skill content；12资源fixture证明只暴露10项并排除SKILL.md；
- 完整回归 `1004 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未调用付费/公网Provider，未运行SWE-bench。

### 81.7 学习重点

1. Skill正文中的相对路径只有在base directory同时进入模型上下文时才可执行。
2. ToolOutput metadata不是UI装饰；它让Session replay和实时工具卡共享稳定title/identity。
3. lazy body cache必须在取消检查之后写入，否则重试可能复用一次被中断的半成品。
4. 名称相似不构成源码对应关系：MCP timeout、DeepMap cancel、本地repo search是三条不同生产链。

### 81.8 剩余差距

- InfCode有content-only builtin skill location；NZ bundled skills仍是package filesystem资源，没有单独builtin sentinel分支；
- NZ使用Path.rglob并在yield间取消，单个目录系统调用不可中断；文件排序/hidden/ignore行为不等价于InfCode Ripgrep；
- Skill正文/资源文件没有独立字节预算；当前依赖context总预算，超大SKILL.md仍可能造成单个tool output膨胀；
- frontmatter parser是简化逐行key/value，不等价于InfCode完整Skill schema、warning与多行metadata；
- DeepMap是独立外部知识服务，除非用户决定引入对应产品和真实consumer，否则不把repo-intel改名或伪称DeepMap parity；
- 当前InfCode MCP与LSP都没有ctx.abort下传，二者从generic abort候选移除；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 82. A079：Instruction file enabled state 与控制面

### 82.1 InfCode 参考能力

- `infcode/instruction-files/shared.ts`只允许 `global|project` 两个scope与 `AGENTS.md|CLAUDE.md` 两个根文件，稳定ID为 `scope:filename`；
- `state.sql.ts`以 `(scope, project_id, filename)`为主键保存enabled；global使用空project ID，project使用真实project ID；缺行时默认启用；
- `index.ts`的list只返回实际存在文件，状态读取失败成为warning；runtime `instructionSources()`只投影enabled文件并维持global CLAUDE→AGENTS、project CLAUDE→AGENTS优先序；
- create固定以exclusive方式创建空 `AGENTS.md`并清状态行，状态清理失败则删除新文件回滚；delete删除文件并清状态；setEnabled执行upsert；
- `routes.ts`提供list/create/patch-enabled/delete四个HTTP操作；`watch.ts`为产品客户端监听根文件变化并发布debounced `instruction-file.changed`事件。

### 82.2 NZ-Coder 原有不足

- A074只实现“文件存在即加载”，用户不能临时禁用root instruction；唯一办法是重命名或删除文件；
- instruction loader没有可查询的file info、enabled owner或状态warning，控制面即使出现也没有共享核心API；
- HTTP Session service没有InfCode对应的instruction-files routes，client无法配置；
- trace只有预算和路径，不知道有多少已存在文件被禁用，也不暴露状态读取告警。

### 82.3 实现结果

- 在同一instruction模块增加immutable `InstructionFileInfo`、`InstructionFileWarning`和`InstructionFileListResult`；输入严格限制为两类scope、两类filename，所有目标路径均由受控root和常量文件名构造并做relative校验；
- global状态归属 `~/.config/nz-coder/instruction-file-state.json`，project状态归属 `<workspace>/.nz-coder/instruction-file-state.json`；两者用versioned JSON、进程锁、同目录临时文件、`fsync`、atomic replace和0600权限持久化；
- 缺失状态行默认enabled；损坏、超64KB、schema/type错误不会静默禁用指令，而是warning并默认启用；修改操作拒绝在损坏状态上覆盖，create/delete的row清理可以重建这份仅含两个合法键的状态；
- `list_instruction_files()`、`create_instruction_file()`、`set_instruction_file_enabled()`、`delete_instruction_file()`构成共享核心控制面；create使用exclusive open并在状态清理失败时回滚文件，delete同时清对应状态；
- A074 discovery改为先读取global/project file state，只把enabled root files送入预算和reminder producer；rules目录维持既有迁移/加载能力，不受root toggle错误波及；
- `InstructionBundle`与 `instruction_context` trace新增disabled count和warnings；下一次模型请求重新加载状态，因此HTTP修改无需重启Agent；
- loopback HTTP增加与InfCode同形的GET/POST/PATCH/DELETE instruction-files routes，并通过既有authorized workspace ID选择project；标准库client增加完整四操作。

### 82.4 关键设计决策

1. **对齐行为主键，不复制数据库依赖**：InfCode使用共享SQLite；NZ全局状态文件天然对应global空project ID，workspace本地状态文件天然对应project ID，在“不增加依赖”约束下保留相同scope隔离和默认语义。
2. **loader是最终consumer**：只有HTTP返回enabled但模型仍加载文件不算闭环；本轮测试直接断言disable后runtime reminder消失。
3. **损坏时fail-open到authoritative instruction**：这与InfCode `state[filename] ?? true`的安全方向一致；配置损坏不应悄悄绕过项目约束。
4. **root文件和rules不混为一类**：当前InfCode instruction-file CRUD只管理两个根文件；NZ额外rules是已有能力，本轮不伪造任意rule CRUD。
5. **不为无实时UI的consumer常驻watcher**：NZ每次请求都会重新加载，Agent正确性不依赖watch；InfCode watcher属于GUI/IDE配置刷新链，仍明确保留为产品面差距。

### 82.5 关键文件

- `nz_coder/state/instructions.py`：file info/state schema、原子持久、CRUD、enabled discovery过滤和runtime metadata；
- `nz_coder/http_service/server.py`：authenticated workspace-scoped instruction routes与PATCH支持；
- `nz_coder/http_service/client.py`：四类控制操作和安全路径/query编码；
- `nz_coder/runtime/loop.py`：disabled/warning trace consumer；
- `tests/test_instructions.py`：默认、持久过滤、scope隔离、损坏恢复、CRUD row清理和非法输入；
- `tests/test_http_service.py`：独立authorized workspace上的create→write→disable→runtime过滤→delete完整闭环。

### 82.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件 `ruff check`与 `git diff --check`通过；
- Instruction/HTTP聚焦回归 `52 passed`；
- 完整回归 `1010 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- HTTP集成测试通过真实loopback server、Bearer client和独立authorized workspace执行完整CRUD；
- 未调用公网/付费Provider，未运行SWE-bench。

### 82.7 学习重点

1. “支持AGENTS.md”与“对齐instruction系统”不同；真正链路还包括状态owner、控制API、runtime过滤、错误语义和可观测性。
2. 缺省启用是schema语义，不只是UI默认值；状态缺行和状态读取失败都必须定义模型最终看到什么。
3. 文件系统操作与状态写入跨两个owner，create至少要在状态失败时回滚新文件，避免表面成功、实际语义不确定。
4. Python JSON实现可以与TypeScript/SQLite源码行为对齐，但必须把存储差异和并发边界写明，不能称为字节级复制。

### 82.8 剩余差距

- NZ没有InfCode `fs.watch`→150ms debounce→Bus `instruction-file.changed`实时事件；当前没有GUI/IDE设置consumer，HTTP修改由下一请求自然生效；
- InfCode enabled state位于统一数据库并以显式project ID为键；NZ使用global/project两个原子JSON owner，跨进程并发只有atomic replace、没有数据库级compare/update锁；
- HTTP路径/query为了NZ多workspace授权加入 `workspace_id`，并非InfCode optional directory middleware的逐字协议；
- create/delete是低层API，当前终端没有instruction picker或删除确认；用户已要求CLI后置，因此不在本轮伪造半套终端产品；
- NZ额外rules目录没有InfCode rule migrator的完整配置schema、结构化invalid-rule warning或控制面；
- instruction读取仍为串行文件I/O，tracked cache仍是进程级；本轮不证明完整Agent Core或SWE-bench分数对齐。

## 83. A080：GlobTool → Ripgrep.files 结果语义

### 83.1 InfCode 参考能力

- `tool/glob.ts`接收必填pattern和可选path；absolute pattern按第一个glob元字符前的目录拆成search root与相对pattern，普通path相对instance directory解析；
- search path若是文件立即失败；外部目录经过独立permission检查；
- `file/ripgrep.ts`的files producer执行 `rg --no-config --files --glob=!.git/* --hidden [--glob=PATTERN] .`，删除环境中的 `RIPGREP_CONFIG_PATH`，支持abort与30秒timeout；
- Glob consumer从rg stream只取 `limit + 1`即101项，逐项stat得到mtime；超过100时先截为100，再稳定地按mtime降序排序；这不是扫描全仓后选mtime最新100项；
- 输出是absolute file path；空结果固定为 `No files found`，截断追加可执行提示；ToolOutput title是search相对worktree路径，metadata是 `{count, truncated}`；
- 当前源码的glob参数顺序有实际语义：用户正向glob位于 `.git` exclusion之后，ripgrep last-match-wins会使 `*.py`重新包含被ignore的Python文件及 `.git/*.py`。本轮用真实rg验证，而不是继续沿用“必然遵守gitignore”的旧印象。

### 83.2 NZ-Coder 原有不足

- `glob_search`用 `Path.rglob('*')`扫描后自行fnmatch，同时返回文件和目录；InfCode只消费 `rg --files`的文件；
- 无slash的 `*.py`被错误限制在仓库根，而ripgrep globset会递归匹配所有basename；
- 旧结果先全量收集、去重并按字典序排序，然后取100；与InfCode“producer前101→截100→mtime排序”不同，大仓速度和候选集合都会变化；
- 输出相对路径，空结果和截断文本不同，也没有title/count/truncated metadata；
- 无path参数、brace glob、absolute pattern拆分或search-root文件错误；
- A076虽给Path遍历增加取消点，但没有有界rg进程producer，因此仍是正确性/性能半链。

### 83.3 实现结果

- 新增 `_run_rg_files()`：优先解析PATH上的rg，使用与InfCode一致的核心argv和无 `RIPGREP_CONFIG_PATH`环境；stdout由独立reader流入最大128项queue，主线程每20ms检查cancel和统一30秒deadline；
- 第101个结果到达后立即终止并等待rg，不继续扫描全仓；cancel/timeout同样先terminate、超时再kill并等待，reader thread在返回前收口；stdout提前EOF后的process wait也受同一deadline限制；
- `glob_search(pattern, path='.')`新增可选search root，拒绝文件和不存在目录；absolute pattern在workspace内按InfCode算法拆分，workspace外保持NZ全局路径安全约束并返回Error；
- 结果只保留文件，stat失败mtime为0，absolute paths按mtime降序稳定排序；返回str-compatible ToolOutput及InfCode同形title、count、truncated和提示文本；
- 无rg环境使用cooperative `os.walk` fallback；支持basename递归、`**/`零目录、`?`/字符类、bounded brace expansion和leading `!`；仍只取前101，不退回全量收集；
- tool schema和description加入path及mtime排序说明，保留现有公开工具名 `glob_search`与read-effect调度合同；
- 真实rg fixture确认hidden文件进入，并确认当前InfCode argv顺序下正向glob对ignore/`.git` exclusion的覆盖行为。

### 83.4 关键设计决策

1. **对齐stream窗口而非“更聪明”的全局top-100**：全量stat后取最新100看似更好，却改变InfCode的成本上界和候选集合；源码级对齐必须保留先take后sort。
2. **系统rg优先、标准库fallback**：InfCode随产品分发rg；NZ当前wheel没有合法bundled binary资产，不能声称已复制分发链，也不能让缺rg导致核心工具不可用。
3. **workspace安全高于外部目录字面一致**：InfCode有external-directory permission owner；NZ全局约束要求工具路径不逃逸workspace，在没有同构permission consumer前不开放absolute outside路径。
4. **按实际argv行为记录ignore语义**：源码里写了 `.git` exclusion不代表最终行为；后置用户positive glob会覆盖它，测试和文档必须以真实rg证据为准。
5. **不改公开工具名**：`glob_search`已被prompt、SWE/子Agent和恢复策略引用；对齐内部producer/output不需要破坏NZ稳定接口改名为glob。

### 83.5 关键文件

- `nz_coder/tools/search.py`：rg files producer、bounded queue、timeout/cancel、fallback globset、path解析、mtime与ToolOutput；
- `tests/test_search_cancellation.py`：cancel/settle、recursive basename、files-only、mtime/metadata、brace/path/empty、producer-window truncation、absolute safety和真实rg argv语义；
- `tests/test_smoke.py`：现有Agent工具注册与recursive glob回归。

### 83.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件 `ruff check`与 `git diff --check`通过；
- Search/Smoke聚焦回归 `54 passed`；其中Search文件14项覆盖真实慢rg取消和真实rg hidden/ignore顺序；
- 本仓真实smoke：`glob_search('*.py', 'nz_coder/tools')`返回17个absolute文件，title为 `nz_coder/tools`，metadata count=17/truncated=false，mtime最新文件在前；
- 完整回归 `1016 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 83.7 学习重点

1. 文件搜索的“结果语义”包含producer停止位置、stat时机、排序窗口和metadata，不只是glob是否能匹配。
2. `*.py`在shell、Python fnmatch和ripgrep globset中的递归语义不同；不能凭函数名猜实现。
3. 子进程stdout EOF与进程退出是两个事件；两个等待都必须进入同一个timeout/cancel owner。
4. ignore规则有顺序和覆盖关系；静态阅读单个 `--glob=!.git/*`不足以证明最终排除行为。

### 83.8 剩余差距

- NZ未在wheel中bundled rg；PATH无rg时fallback不完整支持ripgrep globset转义、嵌套brace、ignore文件和遍历顺序，不能称为二进制/字节级一致；
- InfCode能在独立external-directory permission后搜索workspace外absolute pattern；NZ根据项目路径安全合同拒绝；
- NZ直接terminate单个rg进程；当前rg不派生工作子进程，但没有InfCode scoped process abstraction或跨平台bundled binary矩阵；
- `grep_search`仍是系统grep文本输出与Python fallback，没有对齐 `Ripgrep.search` JSON row、code=2 partial、glob/file/follow与结构化match producer；
- Skill文件采样仍用Path.rglob而未复用本轮rg producer，A078列出的hidden/ignore/order差距仍存在；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 84. A081：GrepTool → Ripgrep.search JSON/partial

### 84.1 InfCode 参考能力

- `tool/grep.ts`公开pattern、可选path和include；空pattern失败；path若为directory就以其为cwd搜索 `.`，若为file或stat失败就以parent为cwd并把相对file作为rg target；
- `file/ripgrep.ts`执行 `rg --no-config --json --hidden --glob=!.git/* --no-messages [--glob=include] -- pattern target`，清除 `RIPGREP_CONFIG_PATH`，整体受abort与30秒timeout约束；
- stdout每行严格解码begin/match/end/summary JSON union；match保留path、整行text、line_number、absolute_offset和每个submatch text/start/end，path去掉前导 `./`；
- exit code 0正常、1固定空items、2保留已解码items并标记partial；其他code才是RipgrepError；
- Grep把row path变成absolute，批量stat不同文件并过滤不存在/目录，再给每行附文件mtime；所有matching lines按文件mtime降序稳定排序；
- 默认最多显示100个matching rows；整行超过2000字符截断并加 `...`；输出按absolute file path分组为 `Line N: text`，partial追加inaccessible提示；title为pattern，metadata为matches/truncated；
- InfCode Grep的match计数是matching-line row数量，不是regex submatch数量，也不是文件数量。

### 84.2 NZ-Coder 原有不足

- 原实现调用传统GNU grep文本模式，`-l/-c/-n`各走一条字符串解析分支；没有统一结构化producer；
- 默认只返回files_with_matches，模型必须再Read一次；这与InfCode直接返回定位行不同并增加Agent轮次；
- grep exit code 2一律失败，无法保留可访问路径的partial rows；
- 无absolute_offset/submatch验证，也不能区分损坏JSON、匹配为空和部分路径不可访问；
- content结果按grep输出顺序，缺少文件stat过滤、mtime排序、100 row和每行2000字符合同；
- 普通grep subprocess虽然A076已可取消，但不是InfCode `Ripgrep.search`协议，include brace/hidden/JSON边界仍不同。

### 84.3 实现结果

- 新增immutable `_RGMatch`与严格 `_decode_rg_event()`：验证match path/lines、非负line/absolute offset、submatch text/start/end，拒绝未知event与损坏JSON；
- `_run_rg_search()`使用InfCode同形rg argv与净化环境；stdout经最大128项queue逐行解析，主线程检查per-call cancel和30秒deadline；异常、取消和timeout先终止/等待process，再收口reader；
- code 1丢弃items并返回empty，code 2保留rows并返回partial；其他非0/1/2 code变为Error；
- `_search_matches()`统一directory和exact-file路径，解析absolute workspace path，缓存每个文件mtime，过滤消失/目录/越界路径，并按mtime稳定排序；
- `grep_search`默认改为content：最多100 rows、2000字符、absolute path分组、精确empty/truncation/partial文本，以及str-compatible ToolOutput title/matches/truncated；
- 既有 `files_with_matches`、`count`、head_limit、offset、context和case_insensitive作为NZ兼容扩展保留，但全部从同一个JSON row producer投影，不再调用三种外部grep模式；context按文件缓存source lines，避免每个match重复读取；
- PATH无rg时使用Python regex producer，保留相同row/partial/mtime/render链；修复旧fallback无条件IGNORECASE的问题，并继续在遍历/逐行边界响应cancel；
- tool description/schema和system prompt同步默认content语义，避免模型仍按旧files-only假设调用。

### 84.4 关键设计决策

1. **默认行为也必须对齐**：如果底层已经解析JSON但默认仍只返回文件名，Agent轮次与InfCode依旧不同；因此本轮将content设为默认。
2. **兼容扩展共享producer**：不删除NZ已有files/count/context参数，但不允许它们继续维护平行GNU grep事实源。
3. **code 2是有数据的终态**：partial不是普通成功，也不是全部失败；先保留rows，再由最终output显式告知模型不可访问路径。
4. **submatch属于协议验证**：当前UI不渲染高亮，但仍解析和校验start/end，防止将“能取line text”误称完整JSON producer。
5. **workspace边界继续优先**：InfCode有external-directory permission；NZ没有同构owner，因此path仍由 `_safe_path()`限制。

### 84.5 关键文件

- `nz_coder/tools/search.py`：JSON schema translation、rg process owner、Python fallback、stat/mtime consumer和三种output projection；
- `nz_coder/runtime/prompt.py`：默认matching-line语义；
- `tests/test_grep_parity.py`：default grouping/mtime、empty/file、100 rows、2000字符、code2 partial、stat filter、invalid JSON、include/case/fallback、compat modes/context与submatch offset；
- `tests/test_search_cancellation.py`：真实慢fake-rg取消、process退出和fallback取消；
- `tests/test_smoke.py`：Agent注册/系统行为回归。

### 84.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件 `ruff check`与 `git diff --check`通过；
- Grep/Search/Smoke聚焦回归 `64 passed`；包含真实子进程code2、损坏JSON、慢rg cancel/settle与无rg fallback；
- 本仓真实rg smoke：在 `nz_coder/tools/*.py` 搜索 `def glob_search`返回1个match、absolute path、精确Line 694、title与metadata；
- 完整回归 `1026 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 84.7 学习重点

1. `rg --json`的match事件对应matching line；一行有多个submatch时matches仍只加一。
2. exit code 2不能套用传统“非零即失败”；协议consumer必须保留partial data及可观察提示。
3. 搜索排序发生在matching rows而非unique files；稳定mtime排序自然保持同文件内部的rg行序。
4. 兼容功能可以保留，但必须投影自同一结构化producer，否则错误码、过滤和排序会再次分叉。

### 84.8 剩余差距

- NZ wheel仍不bundled rg；Python fallback使用Python `re`，与ripgrep regex、binary、ignore、globset和字节offset语义不完全相同；
- 非match begin/end/summary只验证基本envelope，未逐字段复制InfCode完整Stats/TimeStats schema；可信rg正常输出已覆盖，恶意替代binary的严格度仍较低；
- 2000长度按Python Unicode code point计数，InfCode JavaScript `length/substring`按UTF-16 code unit计数，非BMP字符边界可能不同；
- InfCode能经external-directory permission搜索workspace外文件；NZ按全局路径安全合同拒绝；
- context/files/count是NZ扩展，不是InfCode Grep output；虽然共享row owner，不能称为逐字UI协议；
- rg JSON rows仍在内存中聚合后排序，和当前InfCode一致，但极端匹配量没有独立内存上限；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 85. A082：Shared Ripgrep.files → Skill sample

### 85.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/tool/skill.ts`、`packages/opencode/src/file/ripgrep.ts`；
- 核心行为：filesystem Skill以skill目录为cwd调用共享`Ripgrep.files({ hidden: true, follow: false })`，先过滤路径中包含`SKILL.md`的项目，再`take(10)`并解析为绝对路径；Glob与Skill不是各自维护一套子进程和fallback。

### 85.2 NZ-Coder 原有不足

- A080的`Ripgrep.files`逻辑仍位于`tools/search.py`，Skill无法在不形成反向依赖的情况下复用；
- A078的Skill资源采样使用独立`Path.rglob`，没有复用rg的ignore、`.git`排除、30秒deadline和子进程结算语义；
- 独立fallback在`follow=false`时仍可能把symlink file作为资源返回；过滤与十项上限也没有明确形成“filter before take”的协议。

### 85.3 实现结果

- 核心调用链：`glob_search`/`Skill.sample_files` → `runtime.ripgrep.list_ripgrep_files` → system rg bounded producer或标准库fallback → `RipgrepFilesResult`；
- 共享API统一接收cwd、ordered glob patterns、hidden、follow、max_depth、limit、exclude、cancel event与timeout；
- system rg统一使用`--no-config --files --glob=!.git/*`，清除`RIPGREP_CONFIG_PATH`，以有界queue消费stdout，并在取消、超时、达到`limit + 1`或异常时终止、等待进程与reader；
- Skill不传正向glob，保持真实rg的ignore规则；启用hidden、关闭follow，按`"SKILL.md" in path`在计数前过滤，再保留最多10项；
- PATH中没有rg时，标准库fallback复用同一取消/limit/filter合同，并在`follow=false`时排除目录与文件symlink；
- `glob_search`保留公开接口和A080结果排序，只把文件producer切到共享runtime；Skill加载异常仍由工具边界转换为`Error: `字符串。

### 85.4 关键设计决策

- 共享模块放在`runtime`而不是让`state.skills`依赖`tools.search`，避免状态层反向依赖工具注册层及副作用import；
- 由调用方显式传入当前tool cancel event，runtime不读取tools ContextVar，依赖方向保持单向；
- `exclude`必须在`limit + 1`判断前运行，否则前十个结果若包含`SKILL.md`会错误减少可见资源，也会错误标记truncated；
- Glob继续传用户pattern，因此遵循rg ordered glob的last-match-wins；Skill不传用户pattern，因此不会为了枚举资源意外重新包含ignore或`.git`内容；
- 没有逐字复制TypeScript stream实现：Python用线程读取pipe和bounded queue表达相同生命周期合同，同时保留无外部依赖约束。

### 85.5 关键文件

- `nz_coder/runtime/ripgrep.py`：共享files producer、进程结算、glob fallback与typed result；
- `nz_coder/tools/search.py`：Glob薄适配和既有结果投影；
- `nz_coder/state/skills.py`：Skill filter-before-limit资源采样与取消/错误翻译；
- `tests/test_ripgrep_files.py`：共享身份、argv/env、filter-before-limit、fallback和真实rg协议；
- `tests/test_skill_loading.py`：Skill真实rg hidden/ignore/`.git`行为；
- `tests/test_search_cancellation.py`：共享producer取消与fallback回归。

### 85.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件`ruff check`和`git diff --check`通过；
- Ripgrep/Skill/Search/Smoke聚焦回归`73 passed`；
- 真实rg测试确认：没有正向glob时包含hidden普通资源、遵守ignore并排除`.git`；
- 完整回归`1032 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 85.7 学习重点

1. “共用工具”不是共用函数名，而是进程、deadline、取消、limit、fallback和错误结算只有一个owner。
2. Stream操作顺序属于外部协议：`filter → take(10)`不能替换成`take(10) → filter`。
3. rg没有正向glob和追加正向glob的ignore语义不同；Skill与Glob应共享producer但保留不同调用参数。
4. runtime基础能力不应反向依赖工具注册层；cancel context在边界解析后显式注入更容易测试和复用。

### 85.8 剩余差距

- NZ wheel仍未bundled rg，运行时优先使用PATH版本；
- 标准库fallback不读取`.gitignore`/`.ignore`，globset顺序和特殊字符也只做近似，因此只能作为可取消降级路径；
- 共享层当前只收口`Ripgrep.files`；A081的`Ripgrep.search` JSON producer仍在`tools/search.py`，两者尚未共享更底层的scoped process抽象；
- Skill仍保留NZ自己的frontmatter、allowed_tools和缓存合同，尚未逐字段覆盖InfCode全部schema/warning；
- builtin/content-only Skill位置表达与InfCode sentinel仍不完全相同；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 86. A083：Shared Ripgrep.search + scoped process

### 86.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/file/ripgrep.ts`、`packages/opencode/src/tool/grep.ts`；
- 核心行为：`Ripgrep.Service`同时拥有files与search；search按`--no-config --json --hidden --glob=!.git/* --no-messages`执行，完整解码begin/match/end/summary union，并发消费stdout/stderr/exit code；code 0正常、code 1强制空结果、code 2保留items并标记partial，其余失败；signal与30秒timeout包住同一scoped process。

### 86.2 NZ-Coder 原有不足

- A082只共享files，JSON search的Popen、queue、reader、deadline与stop逻辑仍复制在`tools/search.py`；
- files与search使用两个取消异常和两套reader收口代码，后续修复很容易只落在其中一条路径；
- A081只严格验证match事件；begin/end/summary仅判断data是对象，没有覆盖InfCode的PathText、binary_offset、Stats与TimeStats schema；
- 工具层同时承担进程基础设施、协议解码与最终输出，层次边界不清晰。

### 86.3 实现结果

- 核心调用链：Glob/Skill → `list_ripgrep_files`，Grep → `search_ripgrep`，两者 → `_run_ripgrep_lines` → settled process outcome；
- 新增统一`RipgrepCancelled`、`RipgrepSearchMatch`、`RipgrepSearchResult`与内部process result；A082旧取消名保留为兼容alias；
- `_run_ripgrep_lines`成为唯一Popen owner：清理`RIPGREP_CONFIG_PATH`、有界queue读取stdout、临时文件并行承接stderr、检查cancel/deadline、early-stop时terminate/wait/kill、关闭pipe并join reader；
- `search_ripgrep`支持ordered glob、follow、max-count、精确file targets、NZ兼容case-insensitive，并保持InfCode核心argv顺序；
- `decode_ripgrep_event`逐字段验证begin path、match、end path/binary_offset/stats及summary elapsed_total/stats，所有计数要求真正的非负整数；
- code 1即使收到伪造match行也返回空items，code 2保留items并标记partial，其他code使用stderr构造显式失败；
- `tools/search.py`仅保留workspace安全、无rg Python fallback、mtime排序和ToolOutput兼容投影，私有wrapper只负责把runtime cancellation翻译为工具取消。

### 86.4 关键设计决策

- 共享的是生命周期owner，不只是把两个公开函数放进同一文件；files的consumer early-stop与search的完整collect都通过同一runner callback表达；
- stderr直接写临时文件而非无人消费的PIPE，避免大错误输出填满pipe造成子进程死锁；
- decoder位于runtime，因为JSON union是Ripgrep协议，不属于Grep UI；stat/mtime与workspace containment仍留在工具consumer；
- Python fallback不伪装成`search_ripgrep`：共享runtime函数表示真实rg协议，PATH缺失时由NZ工具边界显式选择近似fallback；
- 保留`_run_rg_search`与旧取消名的窄兼容适配，避免破坏现有内部测试/调用，但它们不再拥有进程。

### 86.5 关键文件

- `nz_coder/runtime/ripgrep.py`：files/search typed producer、完整JSON decoder与共享scoped process生命周期；
- `nz_coder/tools/search.py`：runtime适配、fallback、workspace校验、mtime排序和最终输出；
- `tests/test_ripgrep_files.py`：files/search共享身份、argv/env、完整event union、code1/code2及timeout进程结算；
- `tests/test_grep_parity.py`：runtime decoder及Grep结果协议；
- `tests/test_search_cancellation.py`：共享runtime进程取消和上层错误翻译。

### 86.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件`ruff check`与`git diff --check`通过；
- Ripgrep/Grep/Search/Skill/Smoke聚焦回归`77 passed`；
- 真实PATH rg通过默认Grep、hidden/ignore/`.git`及ordered glob既有测试；fake rg覆盖code1丢弃rows、code2 partial、完整argv/env和50ms timeout后process settled；
- 完整回归`1036 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 86.7 学习重点

1. 源码级对齐必须把schema解码放在producer边界，不能等到UI只取部分字段后再假定其他事件合法。
2. `code 1`不仅表示partial=false，还要求丢弃已经收集的items；exit code是结果语义的一部分。
3. files可提前停止、search必须完整收集，但进程取消、超时、stderr和reader结算仍应只有一个owner。
4. fallback与真实协议应明确分层；近似实现可以保障可用性，但不能污染真实producer的语义声明。

### 86.8 剩余差距

- NZ仍没有InfCode的bundled rg解析与多平台二进制发布，只优先使用PATH并允许工具层fallback；
- Python fallback不读取ignore文件，regex/binary/globset与真实rg不完全一致；
- `search_ripgrep`仍把全部match事件收集到内存，与当前InfCode一致但没有极端输出内存上限；
- NZ按workspace路径合同拒绝外部目录，尚未对齐InfCode external-directory permission；
- 2000字符输出按Python Unicode code point截断，InfCode按JavaScript UTF-16 code unit；
- `Ripgrep.tree`只有InfCode debug consumer，NZ没有为模块完整性制造无产品consumer的接口；
- 本轮不证明完整Agent Core或SWE-bench分数对齐。

## 87. A084：Provider stream内工具执行主链

### 87.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/prompt.ts`的`resolveTools()`、`packages/opencode/src/session/processor.ts`的stream event switch；
- 核心行为：工具以带`execute(args, options)`的对象交给AI SDK；processor在同一stream中消费`tool-input-start/delta/end → tool-call → tool-result/tool-error → finish-step`，Tool Context携带abort、call ID、metadata和permission owner；工具完成后stream仍可给出usage/finish事件。

### 87.2 NZ-Coder 原有不足

- `_call_streaming()`只聚合tool-call，完整返回`LLMResult`后，外层Loop才调用`_execute_tools_async()`；Provider stream与工具副作用是两个相邻但分离的阶段；
- `session.message.completed`发生在dispatch之前，和InfCode“tool事件属于stream、message完成在后”的顺序不一致；
- 若直接把执行塞入同步Provider worker，会阻塞async permission/question/cancellation；若简单提前执行，又会漏掉常见的尾部usage chunk；
- 工具副作用完成后若stream尾部报错，原重试逻辑可能重发同一模型请求并重复执行写入；
- Provider worker等待工具的时间会混入LLM latency，trace无法区分模型等待和本地执行。

### 87.3 实现结果

- 核心调用链：Provider stream worker识别闭合tool-call → `_StreamToolBridge`用`run_coroutine_threadsafe`回到主event loop → `_materialize_llm_result`持久化assistant/ToolPart → `_execute_tools_async(..., finish_step=False)` → stream继续消费尾chunk → reconcile usage/metadata → finish-step；
- 带finish reason的Provider在tool-call chunk后立即执行；没有显式finish event的normalized Provider在stream EOF、但仍在stream owner返回前执行；
- 工具开始前等待step-start snapshot，并把assistant消息和完整输入先持久化；工具完成只结算ToolPart，不提前写step-finish；
- stream尾部结果通过`_reconcile_materialized_llm_result`只更新assistant文本、reasoning、usage和Provider metadata，绝不重新`register_tool_calls`，因此completed ToolPart不会倒退为pending；
- 工具执行后继续读取尾部usage，最终assistant `_nz_usage`和StepFinish tokens使用完整数据；
- 工具已经完成后发生Provider stream错误时，记录`provider_stream_error_after_tools`并以error终止，不进入API retry，从根源避免写副作用重放；
- 外层取消会先cancel bridge中的async batch；既有settled-worker和事务rollback收口后，Provider worker收到内部cancel信号并退出；stream的`close()`在所有正常、失败和取消路径best-effort执行；
- LLM duration扣除bridge等待时间，另以`stream_tool_wait_ms`记录本地工具等待，既有tool batch trace继续保存真实工具耗时；
- 非streaming模式保持原调度链，避免改变SWE-bench runner的确定性接口。

### 87.4 关键设计决策

- 没有伪造AI SDK：NZ四类Provider都输出统一chunk但不接收可执行Tool对象，因此使用“同步stream worker→async event loop”的窄桥翻译同一生命周期语义；
- ToolExecutor仍是唯一permission、hook、事务、并行和取消owner；bridge不复制工具执行逻辑，只改变它在Provider生命周期中的调用位置；
- ToolPart完成与StepFinish分开：前者发生在tool-result，后者必须等待stream尾usage，这个顺序是完整协议而非UI细节；
- 一旦本地副作用发生，后续stream错误不可按网络错误重试；宁可显式error并保留可审计完成态，也不能重复写文件或再次提问；
- materialize与reconcile分离，保证工具执行所需assistant/tool-call历史先存在，同时禁止尾chunk覆盖已结算工具状态。

### 87.5 关键文件

- `nz_coder/runtime/loop.py`：LLMResult stream状态、sync→async bridge、stream内dispatch、materialize/reconcile、post-tool错误与trace；
- `tests/test_session_events.py`：事件顺序、工具早于尾usage、usage协调、写后stream错误不重试、bridge取消和stream close；
- 既有`tests/test_loop_fake.py`、`test_session_processor.py`、`test_tool_cancellation_context.py`、`test_cancellation_safety.py`继续验证事务与终态。

### 87.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件`ruff check`与`git diff --check`通过；
- Agent/Session/取消/Bash聚焦`100 passed`；Provider/上下文/附件/Question组合`121 passed`；合并组合`221 passed`；
- 受控stream证明tool result发生在尾usage producer继续前，StepFinish和assistant usage最终均为3/2/5；
- 受控写工具后stream故障证明只发送一次Provider请求、只写一次文件、ToolPart保持completed而StepFinish为error；
- 取消测试证明async handler finally完成且Provider stream被close；
- 完整回归`1039 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 87.7 学习重点

1. “stream内执行”不等于在同步读取线程直接调用工具；permission、question和取消要求副作用回到async runtime owner。
2. tool-result与finish-step之间可能还有usage事件，提前finish会永久丢失真实token数据。
3. 有副作用后的网络错误不能复用普通Provider retry策略；重试边界必须知道工具是否已经执行。
4. 最终结果协调只能更新模型字段，不能重新建立ToolPart，否则状态机会从completed倒退到pending。
5. LLM trace必须扣除本地工具等待，否则模型慢与工具慢无法区分。

### 87.8 剩余差距

- NZ没有AI SDK原生`tool-result/tool-error`事件对象；当前由共享ToolExecutor直接驱动同一SessionProcessor，属于生命周期语义对齐，不是库实现同构；
- 非streaming评测路径仍在完整response后执行工具；它保留稳定runner接口，但不属于InfCode交互stream主链；
- Provider-executed tools尚无真实adapter producer，因此`providerExecuted`只支持metadata保存，没有远端工具事件闭环；
- 工具调用必须等完整可解析arguments；除`filePath`预览外不会对半截JSON执行，和InfCode/AI SDK一致；
- Python线程中的底层同步Provider socket仍不能被强杀，只能通过client/stream close与settle边界协作取消；
- 本轮不证明动态Provider生态、完整Session数据库/分支模型或SWE-bench分数对齐。

## 88. A085：可安装 Provider adapter 运行链

### 88.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/provider/models.ts`、`provider/provider.ts`的`fromModelsDevModel()`、`resolveSDK()`和`getLanguage()`；
- 核心行为：模型记录保留Provider、API模型ID和adapter package三层身份；请求时优先解析内置SDK，否则安装/导入模型指定package，创建并缓存SDK，再由model loader取得真实language model。

### 88.2 NZ-Coder 原有不足

- models.dev同步只提供能力数据，`create_provider()`最终仍被OpenAI-compatible、Responses、Anthropic和Gemini四组硬编码名称截断；
- 即使外部Python包实现了`ModelProvider`，workspace模型选择也拒绝未知Provider，因此无法进入AgentLoop；
- 没有adapter冲突、导入失败、contract缺失或版本不兼容的归因边界；
- 若简单枚举entry point并立即load，启动/doctor会执行所有已安装第三方代码，扩大供应链风险。

### 88.3 实现结果

- 定义`nz_coder.providers` Python entry point组和版本1 factory contract；factory接收`provider_name/api_key/base_url/client_factory`并返回完整`ModelProvider`；
- `installed_provider_extensions()`只读取distribution metadata，不调用`load()`，可安全列出Provider ID、target和distribution；
- `create_provider()`保持内置adapter优先，未知Provider才精确查找同名entry point；零个回到原未知Provider错误，多个声明者在导入前拒绝；
- 仅明确选择的entry point会被import；导入、API版本、factory初始化和返回对象方法分别校验并保留Provider归因；
- `save_model_selection()`接受恰好一个已安装adapter的Provider ID；随后`active_model_selection → create_provider → provider.create_client`进入真实AgentLoop；
- doctor将第三方adapter的类型/版本错误显示为model失败，不再让诊断命令崩溃；
- 第三方entry point不能覆盖任何内置Provider名称。

### 88.4 关键设计决策

- 对齐的是动态adapter生命周期，不复制TypeScript、Bun或AI SDK；Python发行版的标准互操作边界是`importlib.metadata` entry point；
- 不实现InfCode的请求时自动NPM安装：编码Agent静默下载并执行registry指定包风险过高，NZ只加载用户已经安装且明确选中的包；
- discovery与execution严格分离，保证列举模型、doctor和错误提示不会顺带执行第三方初始化；
- 不缓存adapter实例：Provider/client仍由每个Agent owner创建，避免把连接和workspace状态提升成进程全局单例。

### 88.5 关键文件

- `nz_coder/providers/extensions.py`：entry point发现、单一所有者解析、版本/接口校验和factory调用；
- `nz_coder/providers/__init__.py`：内置优先、动态fallback和真实Provider工厂入口；
- `nz_coder/providers/models.py`：workspace选择接受已安装adapter；
- `nz_coder/doctor.py`：第三方contract错误的离线诊断收口；
- `tests/test_provider_extensions.py`：无执行发现、连接参数、冲突、导入/接口/版本失败、内置防覆盖和AgentLoop端到端消费。

### 88.6 验证结果

- `compileall`、目标文件`ruff check`和`git diff --check`通过；
- 新增9项adapter测试；Provider/模型/上下文/Agent相关组合`133 passed`；
- 工作区选择`acme/code-model`后，受控entry point只加载一次，AgentLoop持有返回adapter并由它创建唯一client；
- 全部测试因单执行通道约25秒上限按文件名分片，七组共`1048 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未访问公网registry或付费Provider。

### 88.7 学习重点

1. 动态Provider不是扩大一个名称集合，必须从选择、加载、contract验证一路接到实际Agent请求owner。
2. entry point metadata发现不等于import；把二者分开能让doctor和列表保持无副作用。
3. 内置优先与重复所有者拒绝是确定性要求，也防止恶意包通过同名entry point劫持常用Provider。
4. 自动安装adapter虽方便，但会把远程registry变成代码执行入口；本地终端Agent应要求用户先完成安装和明确选择。

### 88.8 剩余差距

- InfCode可按模型记录中的`api.npm`运行时安装NPM包并支持`file://` adapter；NZ只支持已安装Python distribution，不自动下载、不加载任意本地文件；
- NZ registry尚未完整保留/消费逻辑model ID与Provider API model ID的分离，也没有每模型adapter覆盖；这是下一里程碑；
- 第三方adapter还没有专属模型发现hook、auth plugin、unload/hot-reload或进程隔离；凭据仍复用Provider connection合同；
- 没有用真实外部distribution做wheel互操作，只完成受控entry point生产链验证；
- 本轮不证明20余Provider逐一协议等价，也不证明SWE-bench分数。

## 89. A086：逻辑模型与Provider API模型身份分离

### 89.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/provider/models.ts`的Provider/Model schema、`provider/provider.ts`的`fromModelsDevModel()`、config provider merge、`resolveSDK()`和`getLanguage()`；
- 核心行为：`model.id`是产品和Session中的逻辑身份，`model.api.id`是实际传给SDK的模型/部署ID，`model.api.npm/url`分别决定adapter和endpoint；config可覆盖这些字段而不改变逻辑选择名。

### 89.2 NZ-Coder 原有不足

- registry把map key和记录内`id`合并成单一`model_id`，逻辑别名被丢失；
- 带`/`的合法上游模型/部署ID被过滤，OpenRouter或部署式endpoint会漏项；
- AgentLoop用同一`self.model_id`同时承担界面、capability lookup和Provider wire参数，无法支持别名；
- planning、replanning、memory rerank/extract、stream/non-stream和子Agent存在多个调用点，单改主请求会形成隐蔽分叉。

### 89.3 实现结果

- registry以map key保存逻辑`model_id`，另保存记录内`api_model_id`，并保留模型级覆盖优先的`adapter`和`endpoint`元数据；旧snapshot缺字段时确定性回退逻辑ID；
- 模型ID允许有界、无控制字符的`/`，不再把部署路径当文件路径过滤；
- `registry_runtime_model()`按provider/logical ID返回不可变wire记录，找不到时返回None而不影响环境/本地目录路径；
- AgentLoop保留`self.model_id`作为capability、Session、trace和产品身份，新增`self.request_model_id`作为Provider wire身份；
- `_active_model_id()`成为所有模型调用的单一wire resolver，并保持`__new__`测试fake向后兼容；
- 主stream/non-stream、planner/replanner、memory rerank/extract和父→子Agent继承统一使用wire ID；run trace同时记录logical `model`与`request_model`；
- 已安装第三方Provider可出现在registry中，但metadata发现仍不执行entry point。

### 89.4 关键设计决策

- capability继续绑定逻辑ID，因为精确registry记录以用户选择名索引；若先替换成API ID会丢失context/output/tools等模型元数据；
- Session和终端展示逻辑ID，只有网络请求与继承的模型执行ID使用wire值，避免UI突然显示部署内部名称；
- 保存但暂不消费registry endpoint：自动把API key发送到远程目录声明的URL会产生凭据外泄风险，endpoint切换仍须显式Provider连接配置；
- 保存adapter元数据不等于自动加载它；A085的“已安装且明确选择”安全边界继续有效。

### 89.5 关键文件

- `nz_coder/providers/registry.py`：logical/API/adapter/endpoint归一化、旧快照fallback和runtime lookup；
- `nz_coder/runtime/loop.py`：双模型身份、唯一wire resolver及所有内部模型调用consumer；
- `tests/test_model_registry.py`：别名、带slash API ID、覆盖元数据与真实non-stream请求；
- `tests/test_provider_extensions.py`：未知但已安装Provider进入registry且不被import。

### 89.6 验证结果

- `python -m compileall -q nz_coder tests`、目标文件`ruff check`、`git diff --check`通过；
- registry/Provider/Loop/subagent/memory组合`169 passed`；
- 受控链证明逻辑`friendly-code`获得123K registry capability，而实际Provider kwargs为`deployments/team/code-model`；
- 全部测试按执行时限分片为222、69、156、248、356项，共`1051 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未访问公网或付费Provider。

### 89.7 学习重点

1. 模型“名字”至少有产品身份和wire身份两层；混用会让目录、能力策略和真实请求互相破坏。
2. 修主请求不够，planner、memory和subagent等旁路必须经过同一resolver，否则同一Session会随机使用不同模型ID。
3. 目录提供的endpoint和adapter是代码/凭据路由输入；保存元数据可以自动化，消费它必须有更强的信任边界。
4. `/`是云模型与部署ID的正常字符，不应套用workspace路径安全规则。

### 89.8 剩余差距

- registry的`adapter`与`endpoint`目前只保存和展示内部记录；不会自动改变连接或加载模型级adapter；
- InfCode config可为单个model覆盖ID/npm/api/options/headers，NZ尚无等价的本地Provider配置schema与merge优先级；
- subagent trace当前继承wire ID，尚未同时保存parent logical identity；
- registry仍只导入内置别名和已安装扩展Provider，不是InfCode完整Provider数据库；
- 未完成真实外部Provider别名请求互操作与SWE-bench证据。

## 90. A087：原生Provider finish/usage与输出上限终态

### 90.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/processor.ts`的`finish-step`、`packages/opencode/src/kilocode/session/processor.ts`的`lengthWarning()`、`providerFinishError()`和prompt结束判断；
- 核心行为：Provider stream终态携带finish reason、usage和metadata；Session保存tokens与finish；`length`增加不回放给模型的可见警告，reasoning-only使用更强提示；Provider无细节`error` finish写API error并以error关闭。

### 90.2 NZ-Coder 原有不足

- Anthropic、Gemini和OpenAI Responses原生adapter只归一化text/reasoning/tool call，完全丢弃stop/finish reason与usage；
- Agent因此把`max_tokens`默认为`stop`，StepFinish tokens为0，基于Provider usage的上下文预算也失去输入；
- reasoning耗尽输出窗口且没有正文时，终端看起来像模型空响应；
- Provider明确以error finish结束但没有抛异常时，NZ会按普通成功返回；
- 非stream response若带tool call同时length，旧Loop仍可能执行不完整参数产生副作用。

### 90.3 实现结果

- normalized completion/chunk的choice增加`finish_reason`，response/chunk增加OpenAI-shape usage；既有字段和调用保持兼容；
- Anthropic映射`tool_use→tool_calls`、`max_tokens→length`、正常stop；stream从`message_start`保留input、从`message_delta`合并output并发出累计usage；
- Gemini映射`STOP/tool/ MAX_TOKENS/安全及异常终止`，从`usageMetadata`投影prompt/candidate/total tokens；
- Responses从status/incomplete_details映射stop/tool_calls/length/error，non-stream和`response.completed/incomplete` stream都输出usage；只有`max_output_tokens` incomplete作为length，其它incomplete/failed保持异常；
- Agent在任何工具分支之前消费`length/error`终态：未执行工具先结算为error，绝不dispatch半截调用；stream内已经完成的工具保持completed并记录patch；
- `length`写StepFinish和ignored TextPart；reasoning-only使用精确强提示，普通文本/工具使用通用不完整提示，然后结束本轮；
- `error`写`_nz_error`、error checkpoint/trace/run status，不再报告completed；
- message schema保留TextPart的`ignored=true`，使它可展示、可恢复但不污染assistant正文与下一轮Provider历史。

### 90.4 关键设计决策

- 先修Provider producer再加Session告警；若usage/finish仍被adapter丢弃，consumer分支永远无法可靠触发；
- stream usage必须是累计快照：Anthropic input和output来自不同事件，直接逐chunk覆盖会把先到的input归零；
- output-limit下不执行尚未开始的工具，即使arguments恰好能解析；finish已声明输出被截断，副作用安全优先；
- ignored warning作为Session TextPart而不是拼入assistant content，避免下一轮把产品诊断当模型原话；仅final display组合提示；
- 未把所有非STOP Gemini原因模糊成正常停止，安全/格式类终止统一成为可见error。

### 90.5 关键文件

- `nz_coder/providers/normalized.py`：finish/usage中立响应合同；
- `nz_coder/providers/anthropic.py`、`gemini.py`、`openai_responses.py`：各协议终态与usage producer；
- `nz_coder/runtime/session_processor.py`：两类输出上限warning和ignored Part；
- `nz_coder/runtime/loop.py`：length/error唯一消费边界、工具副作用阻断和最终状态；
- `nz_coder/message_schema.py`：ignored TextPart持久投影；
- `tests/test_native_providers.py`、`test_openai_responses.py`、`test_loop_fake.py`、`test_session_processor.py`：协议与Agent闭环。

### 90.6 验证结果

- `compileall`、目标文件`ruff check`、`git diff --check`通过；
- Provider/Context/Session/Loop/取消/子Agent组合`252 passed`；
- 新增7项终态测试，覆盖三协议non-stream/stream usage、max-output incomplete、reasoning-only警告、未执行工具和provider error；
- 全部测试分片为222、72、156、251、357项，共`1058 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench，未调用公网或付费Provider。

### 90.7 学习重点

1. usage不是观测性附属字段，它直接影响下一轮上下文裁剪和压缩判断。
2. finish reason是Agent控制协议；把所有非异常响应默认为stop会掩盖截断、安全阻断与Provider故障。
3. 流式usage可能拆在多个事件中，归一化层必须输出累计值而非互相覆盖的局部值。
4. 对模型不可见、对用户可见的诊断应是`ignored` Session Part，不能混入assistant原文。
5. 截断的工具调用即使JSON看似完整也不应执行，尤其是写操作。

### 90.8 剩余差距

- 未投影cache read/write、reasoning token与cost等完整AI SDK usage维度；NZ StepFinish当前只有input/output/total；
- OpenAI-compatible adapter依赖SDK原生finish/usage形状，尚未对各兼容厂商异常字段做真实互操作矩阵；
- finish reason的供应商枚举会继续演进，未知值目前保守归为error；
- InfCode的provider error对象包含结构化retryable/API字段和bus error关联ID，NZ仍使用`_nz_error`字符串与trace；
- 尚未处理OpenAI-compatible模型把`<think>`混在text delta中的reasoning demux；这是下一Agent Core候选；
- 未运行SWE-bench或公网Provider验证。

## 91. A088：Think-tag增量reasoning demux

### 91.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/infcode/session/think-tag-demux.ts`与`session/processor.ts`的`handleThinkTagEvent()`、`flushThinkTagDemux()`和cleanup；
- 核心行为：每个Provider attempt建立状态机，只把响应开头的`<think>`或`<thinking>`内容路由到ReasoningPart；标签可跨任意delta；闭合后正文去前导空白；结束/异常时flush半截buffer与未闭合reasoning。

### 91.2 NZ-Coder 原有不足

- 只识别Provider独立`reasoning_content`字段；DeepSeek/QwQ等兼容端若把reasoning放进text标签，标签和私有推理会直接显示并写入assistant正文；
- 用整段正则无法安全处理`<thi`、`nk>`、`</th`、`ink>`跨chunk，也无法判断半截opening在EOF时应恢复为普通文本；
- stream中Tool到达时若demux buffer未flush，stream内工具执行看到的assistant历史不完整；
- Session TextPart、ReasoningPart、message delta与最终LLMResult可能形成不同事实源。

### 91.3 实现结果

- 新增与InfCode同状态语义的`ThinkTagDemux`：`detecting/reasoning/text/done`、最长close-prefix保留、两个tag、EOF finish；
- 只在忽略前导空白后的首内容匹配opening；普通正文中的opening保持字面量，孤立/重复closing tag按InfCode清理；
- 未匹配完的opening在EOF恢复为正文；未闭合reasoning在EOF输出剩余buffer并结束ReasoningPart语义；
- streaming每个Provider retry attempt创建独立状态；可见event更新content、message delta与同一TextPart，reasoning event更新同一ReasoningPart；
- 遇到finish+tool call先flush再桥接工具，使A084 stream-local ToolExecutor拿到完整分流历史；正常EOF与异常/取消路径也flush；
- non-stream response经过同一状态机一次push+finish；tagged reasoning与Provider原生`reasoning_content`合并，正文仅保留可见文本；
- reasoning-only stream不会创建空TextPart；最终materialize/reconcile仍复用既有单一Session owner。

### 91.4 关键设计决策

- 逐行翻译状态机而非正则：这是流协议问题，buffer中的半个标签在下一chunk到来前不能提前显示；
- 只识别leading tag，避免用户要求模型输出XML/HTML代码时把正文中的`<think>`误删；
- closing tag在普通正文中仍清理，这是当前InfCode的明确行为，测试固定该边界而不是凭直觉修改；
- demux位于Provider中立Loop而非某一adapter，因为OpenAI-compatible代理是否使用tag由上游模型决定；
- durable Part和LLMResult由同一event消费函数更新，避免终端隐藏了标签但恢复后的Session仍含原文。

### 91.5 关键文件

- `nz_coder/runtime/think_tags.py`：增量状态机、事件合同和完整文本helper；
- `nz_coder/runtime/loop.py`：per-attempt demux、stream持久更新、tool前/EOF/异常flush与non-stream消费；
- `tests/test_think_tag_demux.py`：跨chunk、两类tag、误匹配、未闭合、stream/non-stream和完整Agent Session证据。

### 91.6 验证结果

- `compileall`、目标文件`ruff check`与`git diff --check`通过；
- demux新增9项测试；Provider/Session/Loop/上下文/取消/子Agent组合`237 passed`；
- 完整Agent stream断言assistant content=`Ready`、reasoning=`inspect state`、TextPart/ReasoningPart各唯一且无原始`<think>`；
- 全部测试分片为222、72、156、251、366项，共`1067 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench或公网Provider。

### 91.7 学习重点

1. 跨chunk标记必须保留“可能成为标签的最长后缀”，提前输出一个`<`都会造成无法回滚的UI/Session污染。
2. detection只发生在响应开头，是避免误伤代码文本的核心约束。
3. Tool stream内执行把flush时点提升为正确性问题：工具调用前必须完成assistant历史物化。
4. retry attempt必须拥有独立demux状态，不能把失败连接的半截tag带到新响应。
5. reasoning分流必须同时作用于即时事件、durable Part、assistant历史和最终result。

### 91.8 剩余差距

- 没有单独的reasoning-start/end事件协议，NZ以累计ReasoningPart更新表达同一生命周期；
- tagged reasoning与原生reasoning同时出现时按到达/归一化顺序合并，尚未保存各自Provider metadata；
- 当前严格区分小写tag，与InfCode一致；不支持大写、属性或Markdown fence变体；
- stream retry的旧ReasoningPart仍沿用NZ既有attempt清理边界，尚未像AI SDK一样保存独立provider event IDs；
- 未做真实DeepSeek/QwQ公网流互操作与SWE-bench验证。

## 92. A089：Reasoning/cache详细token链

### 92.1 InfCode 参考能力

- 参考文件：`session/processor.ts`的`Session.getUsage()`与finish-step tokens，Provider SDK中OpenAI-compatible/Responses usage映射，以及`MessageV2` token schema；
- 核心行为：除input/output/total外，usage保存reasoning tokens和cache read/write，进入Session、成本/telemetry与上下文决策。

### 92.2 NZ-Coder 原有不足

- `_extract_usage_tokens()`只读取三项总量，即使OpenAI SDK已经返回nested details也直接丢弃；
- Anthropic cache、Gemini thoughts/cached content和Responses token details在A087归一化时尚未保留；
- LLMResult、trace、assistant `_nz_usage`和StepFinish都没有详细字段；
- message schema把`cache`当数字，无法表达InfCode的`{read, write}`结构。

### 92.3 实现结果

- 通用usage decoder读取OpenAI `prompt/input_tokens_details.cached_tokens`、`completion/output_tokens_details.reasoning_tokens`以及顶层Anthropic-style cache字段；
- Anthropic non-stream/stream保留`cache_read_input_tokens`和`cache_creation_input_tokens`，跨message-start/delta累计不丢失；
- Gemini映射`thoughtsTokenCount`与`cachedContentTokenCount`；Responses映射input/output details；
- LLMResult增加`reasoning_tokens/cache_read_tokens/cache_write_tokens`，stream累计快照和non-stream返回共用；
- llm_response trace记录三项；assistant `_nz_usage`只在非零时增加详细字段，保持旧消费者的三字段相等判断；
- 所有finish-step路径，包括正常文本、stream tool、non-stream tool、length/error与post-tool stream error，都传递详细usage；
- StepFinish tokens使用`reasoning`和`cache: {read, write}`，message projection严格验证非负有限数字；全零时不增加字段，旧Session仍兼容。

### 92.4 关键设计决策

- cache read/write不能压成单值，两者计费、含义和Provider字段不同；
- 详细字段采用可选非零投影，避免把所有历史snapshot和API响应无意义扩展三个零字段；
- total沿用Provider值或input+output fallback，不额外把reasoning重复相加，因为多数Provider的output已包含reasoning；
- 本轮只对齐token事实，不在缺少完整价格/over-200K规则时伪造cost。

### 92.5 关键文件

- `nz_coder/runtime/loop.py`：详细usage解码、LLMResult、累计、trace和所有finish consumer；
- `nz_coder/providers/anthropic.py`、`gemini.py`、`openai_responses.py`：协议字段producer；
- `nz_coder/runtime/session_processor.py`、`message_schema.py`：StepFinish详细tokens与安全projection；
- `tests/test_loop_fake.py`、`test_native_providers.py`、`test_openai_responses.py`、`test_session_processor.py`：端到端字段断言。

### 92.6 验证结果

- `compileall`、目标文件`ruff check`、`git diff --check`通过；
- Provider/Context/Session/Loop/取消/子Agent组合`233 passed`；
- OpenAI-shape受控response证明15 cache-read、2 cache-write、7 reasoning最终进入assistant usage与StepFinish；Anthropic/Gemini/Responses协议字段均有断言；
- 全部测试分片为222、72、156、251、367项，共`1068 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench或公网/付费Provider。

### 92.7 学习重点

1. Provider的output token通常已经包含reasoning，汇总total时不能再次相加。
2. cache creation与cache hit是两个方向，单一cache数字会破坏成本和性能解释。
3. usage字段跨协议命名差异大，应在Provider/中立decoder边界归一化一次。
4. schema演进可通过“非零才出现”保持旧持久数据和严格测试兼容。

### 92.8 剩余差距

- 尚未保存cost；registry虽有源cost数据，但当前normalized snapshot未建立input/output/cache/reasoning与over-200K价格合同；
- cache字段对OpenAI-compatible厂商仍依赖SDK shape，未覆盖各厂商自定义命名；
- Anthropic service tier、Gemini traffic type、Responses accepted/rejected prediction tokens等扩展维度未投影；
- Session summary/HTTP UI尚无详细token展示，只能从Part/trace读取；
- 未做真实账单对账或SWE-bench验证。

## 93. A090：Empty tool-calls终态守卫

### 93.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/kilocode/session/processor.ts`的`guardEmptyToolCalls()`及SessionProcessor cleanup调用；
- 核心行为：assistant finish为`tool-calls`但parts中没有tool时改写为`stop`，防止上层循环或恢复逻辑认为还有工具阶段。

### 93.2 NZ-Coder 原有不足

- Loop碰到空tool list本身会结束，不会立刻死循环，但StepFinish仍可保存`tool_calls`；
- HTTP/Session恢复、timeline或未来调度consumer只看durable finish时会得到与parts矛盾的状态；
- OpenAI SDK常用下划线`tool_calls`，NZ内部部分路径又使用连字符`tool-calls`，只处理一种会漏掉真实响应。

### 93.3 实现结果

- `SessionProcessor.finish_step()`写入前检查当前assistant的全部Part；
- reason为`tool-calls`或`tool_calls`且不存在ToolPart时归一化为`stop`；
- 只要存在pending/running/completed/error任一ToolPart就保留原finish，不干扰真实工具生命周期；
- 受控Agent响应正文`done`+空tool finish只请求一次，run completed，StepFinish stop且没有ToolPart。

### 93.4 关键设计决策

- 守卫放在durable Session owner而非仅修改Loop局部result，保证所有调用路径和恢复数据共享同一不变量；
- 不把空tool finish当Provider error：当前InfCode也降级stop，模型正文仍是有效结果；
- 同时接受两种拼写，是对NZ多Provider normalized边界的必要兼容。

### 93.5 关键文件

- `nz_coder/runtime/session_processor.py`：finish-step空工具守卫；
- `tests/test_session_processor.py`：直接状态机不变量；
- `tests/test_loop_fake.py`：真实Agent消费链。

### 93.6 验证结果

- 目标文件`ruff check`与`git diff --check`通过；
- Session/Loop/Event/schema/think-tag组合`113 passed`；
- 全部测试分片为222、73、156、251、368项，共`1070 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行SWE-bench或公网Provider。

### 93.7 学习重点

1. Loop“碰巧停止”不等于持久状态正确；Session恢复依赖明确的终态不变量。
2. finish reason和ToolPart集合必须一致，否则任何新的consumer都要重复猜测修复。
3. Provider归一化需要兼容协议拼写差异，但持久层应输出一个确定状态。

### 93.8 剩余差距

- assistant message本体没有InfCode同构的结构化`finish/error`对象，主要事实仍在StepFinish与`_nz_error`；
- 尚无Provider finish枚举的统一Enum/typed schema；
- 不包含cost、provider metadata与公开Provider互操作证据。

## 94. A091：Session/worktree物理删除生命周期

### 94.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/session.ts`的递归Session删除和`session.deleted`事件；`packages/opencode/src/worktree/index.ts`的`Worktree.remove()`；`packages/opencode/src/kilocode/worktree-cleanup.ts`的有界重试；`packages/opencode/src/cli/cmd/tui/component/dialog-session-list.tsx`的删除交互；
- 核心行为：删除不是只移除内存索引，而是由Session owner停止冲突活动、清除持久状态、回收隔离worktree并通知consumer。

### 94.2 NZ-Coder 原有不足

- HTTP `DELETE /session/{id}`只移除服务内存对象，重启后会话从JSON再次出现；
- 后台子Agent创建Git/copy worktree后没有对应remove API，长期运行会持续占用磁盘；
- CLI Session picker只能恢复，不能删除；别名、路径逃逸和活跃run删除边界没有形成一个可测试合同。

### 94.3 实现结果

- `WorktreeManager.remove()`只接受`.nz-coder/worktrees`的直接子目录，Git模式执行强制worktree remove/prune并只删除NZ自有`subagent-`分支，copy模式有界重试删除；
- `delete_session()`严格要求真实Session ID，清理该会话JSON、plan、artifact和子Agent state中记录的worktree，再修复`active/latest`别名；
- `BackgroundAgentManager.close()`与process-local dispose入口先取消并等待会话所属后台任务；
- HTTP live Session在同一会话锁内先拒绝active run，再物理删除、发布`session.deleted`并dispose，避免先删磁盘后返回409；dormant Session同样物理删除；
- CLI增加`/delete-session ID`的精确输入确认，Session picker采用同一项两次Ctrl+D确认；删除当前会话时先安全建立替代owner。

### 94.4 关键设计决策

- 删除目标必须由持久Session状态明确拥有，不能用宽泛glob或自动清理现有`.nz-coder`目录；
- 删除接口拒绝`active`、`latest`和任何规范化后变化的ID，避免破坏性操作把别名或逃逸字符串当真实目标；
- 本轮只补“显式删除”的所有权闭环，不擅自给历史数据增加保留期或执行自动GC。

### 94.5 关键文件

- `nz_coder/runtime/worktree/manager.py`：Git/copy worktree安全删除；
- `nz_coder/runtime/agent_manager.py`：Session后台任务组关闭与缓存dispose；
- `nz_coder/state/sessions.py`：持久Session及其owned artifacts删除；
- `nz_coder/http_service/manager.py`：活跃run互斥和HTTP删除事件；
- `nz_coder/interface/commands/handlers/core.py`：终端删除命令和双确认；
- `tests/test_session_lifecycle.py`、`tests/test_http_service.py`、`tests/test_terminal_infcode_commands.py`：所有权、竞态和交互回归。

### 94.6 验证结果

- 相关119项测试通过；目标文件`ruff check`、`compileall`通过；
- 全部`1078 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未删除工作区中已有历史数据；未运行SWE-bench或公网Provider。

### 94.7 学习重点

1. Session CRUD中的Delete必须删除持久事实，否则服务重启会“复活”会话。
2. worktree是Session/子Agent运行时资源，创建方必须有对称、可重试、路径受限的释放链。
3. 破坏性操作的busy check和物理删除必须在同一同步边界内，否则会出现“API返回失败但数据已删”的竞态。

### 94.8 剩余差距

- 尚未实现按容量/年龄的自动retention、dry-run prune和孤儿worktree扫描；
- 不复制InfCode数据库级级联，因为NZ当前持久层是JSON/artifact；
- CLI仍是滚动REPL，Session删除反馈不具备InfCode侧栏的常驻视觉状态。

## 95. A092：running ToolPart终端实时投影

### 95.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`的`ToolPart`及running状态consumer；其输入来自Session的ToolPart更新，而不是独立进度日志；
- 核心行为：工具处于pending/running时界面持续显示当前状态、title/metadata/output，终态到来后转换为唯一完成表示。

### 95.2 NZ-Coder 原有不足

- A038终端renderer虽订阅tool started/completed，但运行期间只显示模型等待，长Bash即使A064已经持续写`message.part.updated`也不会刷新；
- 若直接把每次preview打印到scrollback，会产生大量重复卡片，且形成Session Part之外的第二事实源。

### 95.3 实现结果

- `TerminalRunRenderer`订阅`message.part.updated`，按tool call ID维护pending/running map；
- CLI run期间启动轻量watch task，瞬态区域显示spinner、工具标题、已运行时长和最新Bash preview；
- tool completed/error/interrupted时移除瞬态行，并把原有completed事件投影为单个永久卡片；run终态等队列在最终flush后输出，保持事件顺序；
- `StreamingRenderer`增加最多四行的瞬态status区域，与流式assistant文本共享同一个live owner，结束和close时必定清空。

### 95.4 关键设计决策

- 只消费A055/A064已有durable Part，不新增旁路progress协议；
- running preview只在瞬态区重绘，避免scrollback spam；最终卡片继续沿用原有tool completed投影和详情级别；
- 本轮不把CLI改造成HTTP薄客户端，也不伪装成InfCode全屏OpenTUI。

### 95.5 关键文件

- `nz_coder/interface/run_renderer.py`：Part更新消费、瞬态状态和最终flush；
- `nz_coder/interface/cli.py`：run watcher生命周期与共享live区域；
- `tests/test_run_renderer.py`、`tests/test_smoke.py`：running Bash preview、唯一完成卡片和状态清理。

### 95.6 验证结果

- 相关119项测试通过；目标文件`ruff check`、`compileall`通过；
- 真实PTY启动、`/help`和`/exit`通过，帮助中可见`/delete-session`；
- 全部`1078 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行付费Provider长命令或SWE-bench。

### 95.7 学习重点

1. 产品级进度不是多打印日志，而是把durable running state投影到可更新的同一视觉区域。
2. watcher、renderer和Agent run需要明确的启动、停止、flush顺序，才能避免丢终态或重复卡片。
3. Bash preview已经由工具producer有界化，终端consumer不应重新发明输出采集。

### 95.8 剩余差距

- 没有InfCode OpenTUI的多区域布局、常驻timeline和单卡原地展开；
- 尚未显示Provider retry/network状态和子Agent层级进度；
- CLI仍直接持有AgentLoop，而不是统一Session API的薄客户端。

## 96. A093：Provider RetryPart终端状态

### 96.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/retry.ts`的重试调度；`packages/opencode/src/session/status.ts`的`retry {attempt,message,next}`状态；`packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`的Session status consumer；
- 核心行为：Provider进入backoff时发布可消费的attempt/message/next状态，UI展示当前重试而不是让用户盲等。

### 96.2 NZ-Coder 原有不足

- A055已经由`SessionProcessor.add_retry()`持久化RetryPart，A052 trace也记录retry，但终端renderer忽略所有非ToolPart更新；
- API限流、5xx或超时backoff期间，用户只能看到普通“Waiting for model”，无法区分模型慢和已知重试。

### 96.3 实现结果

- `TerminalRunRenderer`从既有`message.part.updated`识别RetryPart；
- 瞬态状态显示attempt、距`next`的倒计时和控制字符安全的错误摘要；倒计时结束后显示retry now；
- 任意后续assistant Part、tool started或run终态都会清除瞬态retry；durable RetryPart仍保留在Session历史和trace中；
- 每次刷新只更新共享Live区域，不产生永久重试日志或第二状态协议。

### 96.4 关键设计决策

- NZ没有为了UI另造`session.status` owner，而是复用已经由Agent core持久化并经schema验证的RetryPart；
- 历史RetryPart和“当前正在重试”语义不同，因此consumer只把最新retry保持到下一次真实进度；
- 错误摘要和倒计时均有界，避免Provider异常内容撑高终端。

### 96.5 关键文件

- `nz_coder/interface/run_renderer.py`：RetryPart瞬态投影和清除规则；
- `tests/test_run_renderer.py`：倒计时、无scrollback和后续进度退场。

### 96.6 验证结果

- Session/Loop/renderer/smoke组合`127 passed`；目标文件`ruff check`、`compileall`通过；
- 全部`1079 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行真实限流Provider或SWE-bench。

### 96.7 学习重点

1. durable历史Part可以作为UI事实源，但consumer必须单独定义“何时不再是当前状态”。
2. Provider重试和模型正常首token等待是两种不同产品状态，不能用一个泛化spinner掩盖。
3. 对齐UI行为不要求复制TypeScript状态容器；关键是producer字段、生命周期和清除条件一致。

### 96.8 剩余差距

- 没有InfCode offline/network question状态与自动联网恢复consumer；
- 子Agent运行中仍只显示父`task` ToolPart，未投影子Session当前工具；
- 未用真实Provider 429/Retry-After做付费链路验证。

## 97. A094：Assistant finish/error结构化消息状态

### 97.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/message-v2.ts`的Assistant、AssistantError union和`fromError()`；`packages/opencode/src/session/processor.ts`的finish/error→`updateMessage()`；`packages/opencode/src/cli/cmd/tui/routes/session/index.tsx`的assistant final/error consumer；
- 核心行为：assistant message本体持久保存finish和typed error；StepFinish是步骤证据而不是唯一终态；message更新会实时进入bus consumer。

### 97.2 NZ-Coder 原有不足

- A055–A090把finish写入StepFinish，但assistant info本体没有finish；所有consumer都必须扫描parts；
- 错误只保存在私有`_nz_error`字符串，`message_records()`又过滤所有`_nz_`字段，HTTP snapshot/SSE客户端看不到错误类型；
- 取消、上下文溢出、API失败和普通内部错误无法稳定区分，恢复后只能靠文本猜测。

### 97.3 实现结果

- `SessionProcessor.finish_step()`同时写入assistant内部finish，空tool-call守卫后的规范化结果成为唯一message finish；
- 增加InfCode-style结构化错误union：`UnknownError`、`MessageOutputLengthError`、`MessageAbortedError`、`StructuredOutputError`、`ProviderAuthError`、`ContextOverflowError`和`APIError`；
- 保留`_nz_error`字符串兼容旧测试/内部caller，同时新增typed owner；旧Session根据StepFinish和legacy error自动迁移取消/溢出/Unknown类型；
- Loop明确标注用户取消、context overflow、Provider retry耗尽、client diagnostic、provider finish error和post-tool stream error；
- `message_records()`将finish/error投影到assistant info，save→load→HTTP snapshot保持不变；legacy messages仍只有role/content/tool字段；
- error和finish更新发布`message.updated`，SSE consumer无需等下一次snapshot；消息Part终态先发布，随后发布包含最新info的message update；
- headers/metadata有界到100项，credential-shaped键值统一输出`[REDACTED]`，response body和message有长度上限。

### 97.4 关键设计决策

- 不删除`_nz_error`，避免破坏现有内部接口；typed字段是加法式schema演进；
- 不把所有`error` finish都硬编码成API错误：只有Loop明确知道Provider边界时写`APIError`，无法归因时保留`UnknownError`；
- live事件复用与snapshot完全相同的sanitized info projector，避免SSE和恢复各自维护一套schema。

### 97.5 关键文件

- `nz_coder/message_schema.py`：assistant finish/error schema、迁移、投影、脱敏和`message.updated`；
- `nz_coder/runtime/session_processor.py`：finish owner与live message update；
- `nz_coder/runtime/loop.py`：各失败边界的typed error producer；
- `tests/test_message_schema.py`、`tests/test_session_processor.py`、`tests/test_session_events.py`、`tests/test_http_service.py`：schema、事件、恢复与兼容消费链。

### 97.6 验证结果

- Message/Session/Loop/Event/HTTP组合`148 passed`；目标文件`ruff check`、`compileall`通过；
- 全部`1083 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行公网Provider、付费429/认证错误或SWE-bench。

### 97.7 学习重点

1. Step Part说明“发生过什么”，Assistant info说明“最终是什么”；两者不能互相替代。
2. typed error的价值不只是UI文案，还包括恢复、是否重试、认证引导和可观测性归因。
3. snapshot和live event必须共用一个投影器，否则schema升级必然出现两个事实版本。

### 97.8 剩余差距

- retry耗尽的LLMResult尚未携带原始statusCode/headers/body，最终APIError只能说明已耗尽；
- 未保留InfCode UnknownError的原始class name/code身份；
- assistant cost仍为缺失，ProviderAuthError尚无真实认证失败producer；
- CLI终端只用run failure卡片，尚未针对typed error提供认证/限流专属操作提示。

## 98. A095：Provider异常→typed AssistantError保真

### 98.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/message-v2.ts`的`fromError()`、`unknownIdentity()`、Auth/API/Context error映射；`packages/opencode/src/session/processor.ts`的halt→assistant.error；
- 核心行为：重试调度最终停止时仍保留最后一个真实异常的身份和协议字段，而不是把所有错误压成“重试失败”。

### 98.2 NZ-Coder 原有不足

- `_call_streaming/_call_non_streaming`在重试耗尽后只返回`LLMResult(aborted=True)`，原始exception已经丢失；
- A094只能把最终错误写成泛化`Provider request failed after retries`，statusCode、Retry-After、响应body、异常class/code和认证边界无法恢复；
- post-tool stream error和本地stream tool bridge error都使用同一种APIError分类，归因不准确。

### 98.3 实现结果

- 增加`assistant_error_from_exception()`：401/403且有Provider identity映射`ProviderAuthError`；status/HTTP/API/timeout/connection/rate-limit映射`APIError`；其他映射带原始class/code的`UnknownError`；
- APIError保留statusCode、明确或推导的isRetryable、response headers/body及异常name/code metadata；所有字段继续经过A094统一边界和脱敏；
- `LLMResult.assistant_error`贯穿stream/non-stream的client error、retry exhausted、post-tool provider stream error和本地bridge失败；
- run consumer优先写入原始structured payload，同时继续保留旧`_nz_error="Provider request failed after retries"`兼容caller；
- 本地`_StreamToolExecutionFailed`不再误标成Provider APIError，保持UnknownError；副作用后的Provider stream异常仍标APIError且运行策略禁止自动重试。

### 98.4 关键设计决策

- 错误的`isRetryable`同时表达异常属性和当前Agent决策：已经耗尽或副作用后禁止重试时写false，即使429通常可重试；
- response body只接受字符串/JSON对象并有界保存；header-like对象通过items转换后统一脱敏；
- legacy字符串和typed error可以表达不同粒度：前者保持旧控制流文案，后者面向恢复/UI/协议消费者。

### 98.5 关键文件

- `nz_coder/message_schema.py`：exception→AssistantError分类、身份和协议字段归一化；
- `nz_coder/runtime/loop.py`：LLMResult错误载荷及所有Provider异常出口传递；
- `tests/test_message_schema.py`：429/auth、body和credential redaction；
- `tests/test_loop_fake.py`：真实Agent retry-exhausted→assistant/APIError端到端。

### 98.6 验证结果

- Provider/取消/Loop/Event组合`133 passed`；目标文件`ruff check`、`compileall`通过；
- 全部`1085 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行真实公网429/401、付费Provider或SWE-bench。

### 98.7 学习重点

1. retry决策不能吞掉异常；终止结果需要携带最后一个cause供Session持久化。
2. “异常通常可重试”和“当前步骤还能安全重试”是两层语义，副作用屏障会改变后者。
3. 兼容字段可以保留人类文案，结构化字段负责机器可判定身份，两者无需互相覆盖。

### 98.8 剩余差距

- Provider SDK自定义error shape仍只覆盖常见`status_code/response/headers/body/code`属性；
- 真实Anthropic/Gemini/OpenAI认证和限流响应尚未互操作验证；
- typed error尚未驱动终端的connect/retry/change-model快捷操作；
- assistant cost仍未对齐。

## 99. A096：InfCode-style usage归一化与Assistant cost

### 99.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/session.ts`的`getUsage()`；`packages/opencode/src/session/processor.ts`的finish-step→assistant cost/tokens；`packages/opencode/src/provider/models.ts`和`provider/provider.ts`的models.dev价格映射；`packages/opencode/src/kilocode/session/index.ts`的`providerCost()`；
- 核心行为：先把Provider usage变成不重叠的input/output/reasoning/cache计数，再优先使用Provider报告的真实费用，否则按模型价格和超过200K分段价格计算美元成本，并同时保存到assistant与StepFinish。

### 99.2 NZ-Coder 原有不足

- A089虽然保留了reasoning和cache计数，但input/output仍是Provider原始值；若原始input包含cache、output包含reasoning，直接计费会重复计算；
- A033的models.dev快照只保留capability，丢弃input/output/cache和over-200K价格；
- `LLMResult`、Assistant info、StepFinish和HTTP snapshot均没有cost，OpenRouter返回真实账单时也会被丢弃。

### 99.3 实现结果

- registry快照有界校验并保留USD/百万token的input、output、cache read/write和`context_over_200k`价格，runtime通过不可变`ModelPricing`读取；
- usage统一为`input = raw input - cache read - cache write`、`output = raw output - reasoning`；Anthropic原生usage显式标记其input已是uncached，Gemini和Responses adapter也产生明确uncached input；
- 增加独立确定性价格计算器；当`input + cache read > 200000`时切换over-200K层，reasoning按output价格计费；
- OpenRouter/gateway风格的直接cost、upstream inference cost或market cost存在时优先使用Provider值，否则使用registry估算；
- `LLMResult`保留cost、是否已知和Provider报告值；trace记录cost/source，assistant私有owner、`message_records()` info和StepFinish同步投影；stream内工具提前执行路径与non-stream路径使用同一价格口径；
- 未知价格时不输出cost，而不是把缺少价格误表示为免费；显式零价格仍是已知`0.0`。

### 99.4 关键设计决策

- token四项必须互斥后才能计费，避免cached/reasoning重复收费；Provider total保持原始报告值，不重新拼装；
- Provider实际账单比registry估算更接近用户付款，因此沿用InfCode优先级；只接受有限已知shape、有限非负数值，不把任意metadata写进Session；
- InfCode在未知价格时通过缺省零得到cost=0；NZ选择省略未知cost，避免在面试、统计或账单界面把“没有价格数据”误报成“免费”，这是有意的安全偏差；
- registry声明的endpoint/adapter仍不自动执行或发送凭据，本项只消费经过显式sync落盘的价格数据。

### 99.5 关键文件

- `nz_coder/providers/registry.py`：价格schema、快照校验和runtime模型映射；
- `nz_coder/providers/pricing.py`：基础/over-200K确定性费用计算；
- `nz_coder/providers/anthropic.py`、`gemini.py`、`openai_responses.py`：uncached usage与账单字段保留；
- `nz_coder/runtime/loop.py`：usage归一化、Provider cost优先级、LLMResult/trace/Assistant持久化；
- `nz_coder/runtime/session_processor.py`、`nz_coder/message_schema.py`：StepFinish/Assistant/HTTP cost owner与安全投影；
- `tests/test_model_pricing.py`及Provider/registry/Loop/Session测试：公式、分段、优先级和恢复投影。

### 99.6 验证结果

- 新增/增强7项价格、registry、Provider、Loop、Session测试；相关组合`126 passed`；
- 目标文件`ruff check`和`py_compile`通过；
- 完整回归`1092 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未请求公网registry、Provider真实账单或凭据，未运行SWE-bench。

### 99.7 学习重点

1. usage字段名字相同不代表计数集合相同；计费前必须明确cache/reasoning是否包含在input/output中。
2. 费用是Provider事实、registry估算和未知三态，不应把未知压成零。
3. stream内工具会在Provider流完全结束前消费中间LLMResult，因此价格必须在该中间结果形成时就归一化，不能只在外层run返回后补算。

### 99.8 剩余差距

- Provider实际账单shape只覆盖当前InfCode/OpenRouter/gateway常见字段，尚未用真实公网响应互操作；
- Assistant cost尚未聚合子Agent传播费用，也没有Session级stats/CLI费用汇总consumer；
- registry价格是显式sync的快照，不保证与每个私有兼容endpoint的实际账单一致；
- extra usage维度和币种/税费/组织折扣不在当前模型内。

## 100. A097：前台子Agent递归cost传播

### 100.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/tool/task.ts`的child cost before/after release；`packages/opencode/src/kilocode/session/cost-propagation.ts`的childCost/parent message串行增量；`packages/opencode/src/session/processor.ts`的reconcile与assistant/step cost分离；
- 核心行为：子Session在每条退出路径结算本次新增费用，并发task对同一父Assistant的增量更新不能丢失；父Assistant总费用包含递归子Agent费用，但父StepFinish cost仍只描述父模型当前step。

### 100.2 NZ-Coder 原有不足

- A096只统计主`AgentLoop`；前台`task`使用独立手写子循环，Provider usage没有进入child state或父Session；
- child resume没有“累计总额/本次增量”边界，若直接回传总额会在每次resume重复收费；
- 父Assistant与StepFinish共用一个cost写入动作，无法表达Assistant总费用与本step模型费用的差异；
- background manager允许child先发布terminal status、manager后补`background_result`，观察者存在短暂终态缺字段竞态。

### 100.3 实现结果

- 子Agent每次Provider响应复用A096的token归一化和Provider账单优先级，按child wire model查询显式registry价格，累计`tokens/cost/cost_known`到持久state和trace；
- 每次`task`调用记录进入时child累计cost；所有经统一finalizer的completed、needs_parent、cancelled、timeout、max-turns、rollback和error出口用string-compatible `ToolOutput`返回child session/status、累计tokens、总cost和本次delta；私有调用边界不写入state文件；
- 父工具消费链只接受成功`task`结果的有限正数`child_cost_delta`，`SessionProcessor`在锁内累计child cost并发布live assistant update；
- finish时StepFinish写父模型cost，Assistant info写`parent model cost + child deltas`；多child串行消费不丢增量，unknown child cost不伪造；
- child resume从历史总额继续累计，但只传播本次新增值；子Agent自己的子代仍被禁用，因此当前递归深度是前台一层；
- `BackgroundAgentManager._load()`在worker完成原子result写入前隐藏无result的提前terminal状态，内部worker使用raw读取；终态和`background_result`对观察者同时可见。

### 100.4 关键设计决策

- NZ的子Agent不是InfCode的完整Session service，直接并发修改父Session磁盘消息会与父内存checkpoint竞争；因此以ToolOutput metadata作为task返回事务边界，由父SessionProcessor成为唯一Assistant cost写owner；
- child state保存累计总额用于resume和审计，ToolOutput只传播本次delta，防止重放重复计费；
- StepFinish不包含child cost，保持InfCode“usage cost属于本模型step、assistant cost属于整条响应”的语义；
- 后台Agent跨父回合运行，当前不能安全归到启动它的旧Assistant；本项只修复其终态原子可见性，不伪装为后台cost传播完成。

### 100.5 关键文件

- `nz_coder/runtime/subagent.py`：child usage/cost累计、resume delta与ToolOutput metadata；
- `nz_coder/runtime/loop.py`：task结果cost consumer；
- `nz_coder/runtime/session_processor.py`、`message_schema.py`：child cost owner与Assistant/StepFinish分离；
- `nz_coder/runtime/agent_manager.py`：background terminal/result原子观察边界；
- `tests/test_subagent.py`、`test_session_processor.py`、`test_loop_fake.py`、`test_agent_manager.py`：resume、合并、真实task和竞态证据。

### 100.6 验证结果

- 新增3项child resume/processor/真实task端到端测试；Agent/Session/HTTP组合`172 passed`；
- background cooperative-cancel竞态测试连续运行20次通过；目标文件Ruff和`py_compile`通过；
- 完整回归`1095 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未调用付费Provider，未运行SWE-bench。

### 100.7 学习重点

1. 子Agent费用传播是一个并发记账问题，不是把cost附在字符串末尾；必须定义累计值、delta和唯一父owner。
2. Assistant cost与StepFinish cost作用域不同：前者包含下游工作，后者只对应当前模型usage。
3. terminal状态必须和消费者所需的terminal payload原子可见，否则“状态已完成”并不代表结果可读。

### 100.8 剩余差距

- background `agent_manager`跨回合费用尚未定义父Session/Assistant归属与显式领取协议；
- 子Agent当前禁止再调用task/agent_manager，尚无任意深度递归树；
- child pricing依赖当前workspace provider registry，私有子模型endpoint的真实账单仍需Provider报告；
- Session/CLI总费用统计和公网账单校验仍未实现。

## 101. A098：Assistant模型/token身份与Session stats consumer

### 101.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/message-v2.ts`的Assistant `modelID/providerID/cost/tokens`；`packages/opencode/src/cli/cmd/stats.ts`的Session/model/tool聚合和子Agent费用去重；
- 核心行为：每条Assistant自带当时实际使用的模型身份和稳定token结构；统计从持久消息读取，Session总费用包含已传播的child cost一次，模型维度仍用StepFinish/child Session保留各自费用。

### 101.2 NZ-Coder 原有不足

- A096/A097内部已有usage/cost，但`message_records()`只暴露cost，HTTP/恢复consumer看不到per-message provider/model/tokens；
- Session文件顶层`model`只代表保存时选择，无法正确表示一次Session中途切换模型；
- 没有读取持久事实的stats consumer，费用字段可能长期成为无法核对的死数据；
- 若简单汇总父Assistant cost与child state cost，会把已传播的前台child费用计算两次。

### 101.3 实现结果

- Provider响应物化时在Assistant私有owner写入logical model和Provider identity；不把wire alias误当UI/统计模型；
- `message_records()`忽略可伪造的公开同名字段，只从私有owner投影`provider_id/model_id/tokens`；tokens固定含input/output/reasoning/cache read/write，total保持可选Provider值，零维度稳定存在；
- save/load、HTTP snapshot和`message.updated`自动复用同一投影器，旧Session缺字段时显示unknown而不拿顶层当前模型倒推历史；
- 新增只读`aggregate_session_stats()`：按天窗口扫描当前workspace持久Session，统计顶层/child数量、消息、互斥token、模型、工具、平均/中位tokens和费用完整性；
- Session总费用只加顶层Assistant aggregate；父模型费用优先加StepFinish cost，child model费用从child state单列，避免双算；
- background child尚无父Assistant owner，其费用只进入模型证据并单列`unattributed_background_cost`，不加入已归属总费用；
- 终端新增`/stats [days]`，离线读取本地Session；缺价格时明确标`known requests only`。

### 101.4 关键设计决策

- per-message模型身份必须在响应产生时固化，不能由Session当前选择或registry后来状态回填；
- 内部usage继续保持向后兼容的稀疏字典，外部Assistant schema提供稳定零字段，减少HTTP/CLI consumer分支；
- 统计区分“Session归属总费用”和“模型执行费用”：前者用于用户花费总览，后者用于模型分布，二者不能直接再次相加；
- unknown cost不参与总价并降低`complete_cost`，而不是默认为零；background未归属费用显式展示而非暗中挂到启动回合。

### 101.5 关键文件

- `nz_coder/runtime/loop.py`：响应时固化Assistant provider/model/usage owner；
- `nz_coder/message_schema.py`：稳定Assistant tokens和身份安全投影；
- `nz_coder/session_stats.py`：持久Session/child/model/tool/cost聚合与文本renderer；
- `nz_coder/runtime/subagent.py`：child provider identity；
- `nz_coder/interface/commands/handlers/core.py`：`/stats [days]` consumer；
- `tests/test_message_schema.py`、`test_loop_fake.py`、`test_session_stats.py`：防伪、端到端和去重证据。

### 101.6 验证结果

- 新增3项Assistant schema、stats聚合和command测试；相关Session/HTTP/CLI/Agent组合`187 passed`；
- 目标文件Ruff和`py_compile`通过；
- 完整回归`1098 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未访问公网或付费Provider，未运行SWE-bench。

### 101.7 学习重点

1. 模型切换使Session级model字段不再足够，计费和复盘必须使用per-message身份。
2. 传播后的父cost与child自身cost代表同一笔费用的两个视图，统计时必须按目标维度去重。
3. 未知价格、已归属费用、未归属后台费用是三种状态；一个`totalCost`数字无法诚实表达全部边界。

### 101.8 剩余差距

- `/stats`是当前workspace本地Session扫描，不含跨项目全局数据库、project filter或大规模并行查询；
- background费用仍没有显式claim到某一父Assistant，当前只单列未归属金额；
- old Session没有per-message身份时显示unknown，未提供有证据的历史迁移；
- 统计没有导出JSON/HTTP route、交互图表或真实账单对账。

## 102. A099：Assistant turn lineage与完成时间

### 102.1 InfCode 参考能力

- 参考文件：`packages/opencode/src/session/message-v2.ts`的Assistant `parentID`与`time.created/completed`；`packages/opencode/src/session/processor.ts`的finish/cleanup完成时间；
- 核心行为：Assistant不是仅靠数组位置归属turn，而是显式指向触发它的user message；创建和完成时间由运行生命周期owner写入并在恢复/API中稳定存在。

### 102.2 NZ-Coder 原有不足

- Message已有自身ID但Assistant没有parent，timeline/fork/stats只能扫描数组猜测user turn；
- `_timestamp`在Provider结果物化后才写，不能表示请求真正创建时间；completed只能从Step Part间接推断；
- synthetic user诊断夹在同一turn中，简单“最近user”会错误把下一条Assistant挂到内部控制消息。

### 102.3 实现结果

- 主Loop创建Assistant时，在发Provider请求前绑定最近一条非synthetic user的稳定message ID，并写created wall-clock；
- `SessionProcessor`保证缺created的兼容caller也获得创建时间，所有`finish_step`路径统一写completed；取消、context overflow、API error、length、tool与正常stop共用该owner；
- `message_records()`只接受合法内部parent ID和有限非负time，移除可伪造的公开`parent_id/time`后再投影；
- legacy迁移按消息顺序维护最近真实user；assistant已有合法前向parent则保留，无效/缺失时确定性补齐；synthetic诊断不推进parent；
- legacy time优先使用已有私有time，其次`_timestamp`/最早Part start；completed只从最后StepFinish end恢复且不得早于created；无证据不填当前时间；
- save/load、HTTP snapshot、live`message.updated`、stats与timeline均复用相同消息投影。

### 102.4 关键设计决策

- parent必须指向当前Session中已经出现的message，拒绝未来ID、跨Session/伪造字符串；
- synthetic control message属于原真实user turn，不成为新turn parent；这与上下文压缩/诊断注入的既有provenance一致；
- 旧Session时间只做有证据迁移，不用加载时刻伪造成created/completed；
- `_timestamp`继续服务上下文边界兼容，但Assistant time成为Session/API consumer的typed owner。

### 102.5 关键文件

- `nz_coder/message_schema.py`：parent/time schema、legacy迁移和安全投影；
- `nz_coder/runtime/loop.py`：Provider请求前创建lineage/time；
- `nz_coder/runtime/session_processor.py`：完成时间唯一owner；
- `tests/test_message_schema.py`、`test_loop_fake.py`、`test_session_processor.py`：legacy/synthetic/live生命周期证据。

### 102.6 验证结果

- 新增1项legacy lineage/time迁移测试，并增强真实Loop和Processor断言；相关Session/HTTP/Event/Timeline组合`166 passed`；
- 目标文件Ruff和`py_compile`通过；
- 完整回归`1099 passed`，另有1条既有Python 3.13 multiprocessing `fork()` warning；
- 未运行公网请求或SWE-bench。

### 102.7 学习重点

1. 数组顺序是存储形式，不是turn归属协议；显式parent使fork、统计和恢复不依赖脆弱扫描。
2. 内部诊断虽然使用user role进入模型历史，但不能自动成为产品语义上的user turn。
3. created必须在请求前写，completed必须由终态owner写；结果返回时补一个timestamp不能替代生命周期。

### 102.8 剩余差距

- User message尚没有统一typed created time，当前parent目标只有稳定ID；
- Assistant尚未投影InfCode的agent/mode/path/variant/structured/endState全部字段；
- HTTP/fork仍保留部分按数组turn扫描的兼容实现，尚未全面切换为parent图；
- 多分支数据库parent/child Session关系不等同于message parent，本项不声称完整branch graph。

## 103. A100：User创建时间与stats时间事实源

### 103.1 InfCode 参考能力

- `session/message-v2.ts`中User要求`time.created`；Session统计以消息生命周期而非存储文件mtime表达真实活动时间。

### 103.2 NZ-Coder 原有不足

- 只有Assistant拥有typed time；复制、恢复或重写Session文件会使`/stats`日期被mtime污染；child epoch还被费用数值的10亿上限误拒绝。

### 103.3 实现结果

- CLI、HTTP、AgentLoop、hook和child入口为新User写私有created；HTTP公开投影拒绝用户伪造的`time`。
- legacy只从`_timestamp`或Part start恢复时间，无证据不使用加载时刻；stats优先消息created/completed，才回退mtime。

### 103.4 关键设计决策

- 时间由producer写，migration只消费证据；时间戳与费用使用不同数值校验器。

### 103.5 关键文件

- `nz_coder/message_schema.py`、`session_stats.py`、`runtime/loop.py`、`interface/cli.py`、`http_service/manager.py`。

### 103.6 验证结果

- 2项新增测试、128项首轮聚焦；最终全量`1102 passed`；未运行SWE-bench或公网Provider。

### 103.7 学习重点

1. 文件更新时间不是消息创建时间；迁移时“未知”优于伪造当前时间。

### 103.8 剩余差距

- 跨workspace全局统计数据库和远端Session时间同步仍未实现。

## 104. A101：User Agent/model/variant身份

### 104.1 InfCode 参考能力

- `session/prompt.ts`创建User时解析Agent、model与variant，再将其作为本turn后续Loop的权威选择。

### 104.2 NZ-Coder 原有不足

- 历史User只有role/content；恢复后只能读取当前workspace模型，切模型会丢失逐turn身份。

### 104.3 实现结果

- 新User固化`build/plan`、逻辑provider/model和可选variant；CLI和HTTP在首次保存前绑定，AgentLoop补齐评测/兼容入口，child使用自己的agent type/model。
- 私有owner已存在时不被后续当前配置覆写；公开同名字段不进入Session API。

### 104.4 关键设计决策

- Session保存逻辑模型ID而非wire alias；Agent身份表达运行策略，不复用permission mode的全部枚举。

### 104.5 关键文件

- `nz_coder/message_schema.py`、`runtime/loop.py`、`runtime/subagent.py`、CLI和HTTP manager。

### 104.6 验证结果

- schema与真实两步Loop测试增强、147项聚焦；最终全量`1102 passed`。

### 104.7 学习重点

1. 模型选择属于turn，不属于可被以后修改的Session全局配置。

### 104.8 剩余差距

- User `format/system/tools/editorContext`尚无真实产品consumer，未添加空字段。

## 105. A102：Assistant执行身份与workspace路径

### 105.1 InfCode 参考能力

- `MessageV2.Assistant`与`prompt.ts`在请求前写`mode/agent/variant/path.cwd/path.root`，区分工作目录与worktree根。

### 105.2 NZ-Coder 原有不足

- Assistant只有provider/model，无法从恢复数据确认哪个Agent模式、哪个workspace/worktree产生该step。

### 105.3 实现结果

- 正常Assistant与压缩失败错误Assistant均在Provider请求前绑定mode/agent/variant/cwd/root；投影只接受有界私有字段。

### 105.4 关键设计决策

- 主Agent当前cwd/root相同，但协议仍保留两者，避免未来child/worktree消费时破坏schema。

### 105.5 关键文件

- `nz_coder/message_schema.py`、`runtime/loop.py`。

### 105.6 验证结果

- schema与真实Loop断言增强、158项聚焦；最终全量`1102 passed`。

### 105.7 学习重点

1. workspace路径是执行provenance，不应在结果返回后从当前进程状态反推。

### 105.8 剩余差距

- 自定义child loop的每条Assistant尚未全部升级为顶层MessageV2投影。

## 106. A103：RetryPart typed error/time

### 106.1 InfCode 参考能力

- `MessageV2.RetryPart`定义`attempt/error/time.created`；retry schedule另提供`message/next`瞬态状态。

### 106.2 NZ-Coder 原有不足

- 已有RetryPart和断点保存，但持久结构仅有`message/next`，丢失typed Provider错误和重试决定时间。

### 106.3 实现结果

- Provider原异常进入Processor，生成脱敏typed API/Auth/Unknown error和created；`message/next`保留为兼容扩展与终端倒计时consumer。

### 106.4 关键设计决策

- 不删除已有consumer字段；新结构由异常owner生成，不从字符串二次猜status/header。

### 106.5 关键文件

- `runtime/session_processor.py`、`runtime/loop.py`、`message_schema.py`。

### 106.6 验证结果

- Processor/Loop/Event测试增强、117项聚焦；最终全量`1102 passed`。

### 106.7 学习重点

1. “有Retry模块”不等于源码语义对齐，状态展示与持久错误事实是两个层次。

### 106.8 剩余差距

- InfCode offline重连交互尚无对应终端consumer；公网429互操作证据仍缺。

## 107. A104：Assistant endState终态

### 107.1 InfCode 参考能力

- `infcode/session/message-v2.ts`定义四类MessageEndReason；`persistAssistantEndState`只在run退出时标记最终可见Assistant且绝不覆写。

### 107.2 NZ-Coder 原有不足

- StepFinish描述模型step结束，却不能判断整个用户turn是否终结；HTTP/恢复只能结合run状态和数组位置推断。

### 107.3 实现结果

- finalize定位本真实User之后最后一条已结算Assistant，写不可覆写`completed/errored/canceled/interrupted`并发布`message.updated`、checkpoint。
- `MessageAbortedError`优先映射canceled；中间tool Assistant只保留StepFinish，不写endState。

### 107.4 关键设计决策

- endState属于turn final Assistant，不能在每次`finish_step`写；没有当前turn合法target时不回退污染旧turn。

### 107.5 关键文件

- `nz_coder/message_schema.py`、`runtime/loop.py`、`tests/test_session_events.py`。

### 107.6 验证结果

- 1项新增及真实多step/Event测试增强；145项组合、全量`1102 passed`，1条既有Python 3.13 fork warning。

### 107.7 学习重点

1. step完成与turn完成必须分层；终态只能由run exit owner落盘一次。

### 107.8 剩余差距

- `structured`需要User format→动态tool→schema验证完整consumer，当前明确后置；message parent图尚未替代全部数组扫描。

## 108. A105：Session fork身份图与标题语义

### 108.1 InfCode 参考能力

- `packages/opencode/src/session/session.ts`的`Session.fork()`先创建没有`parentID`的新顶层Session，再为截点以前的每条Message和Part生成新ID，重连Assistant `parentID`和compaction `tail_start_id`。
- `getForkedTitle()`把普通标题变为`(fork #1)`，再次fork时递增编号。
- Kilo扩展另有child Session remap；它建立在普通fork之后，不意味着fork本身是parent/child关系。

### 108.2 NZ-Coder 原有不足

- `/fork`只深复制数组，旧Session的Message/Part ID、Assistant parent和typed source引用会进入新Session，破坏全局身份与事件更新边界。
- timeline主要按数组位置归属Assistant，不能可靠处理恢复后或图顺序不同的消息。
- 新fork没有来源标题语义；开发中一度把普通fork误建模为child Session，和当前InfCode源码相反。

### 108.3 实现结果

- `rebind_fork_history()`深复制后为完整Message/Part图重新编号，更新Session ID、Part owner、Assistant parent、summary/source/compaction消息引用及FilePart source Part引用；普通用户文本永不参与替换。
- timeline优先使用Assistant显式parent图，旧Session或无效parent才按位置兼容；工具摘要同时读取legacy tool calls和durable ToolPart。
- Session现在具有InfCode式默认标题`New Session`；fork标题通过同构正则按`fork #N`递增。
- 撤销错误的fork `parent_session_id/fork_message_id/fork_turn`设计和测试，保持task child ownership与conversation fork语义分离。

### 108.4 关键设计决策

- fork必须是新Session身份域内的克隆，而不是复用旧对象ID，也不是旧Session的task child。
- 只重写schema已知的引用字段，不对任意字符串做全局替换，避免把用户代码、日志或提示词中恰好相同的ID改坏。
- NZ保留按可见turn选择fork截点的产品入口；截点选定以后，克隆语义按InfCode消息图实现。

### 108.5 关键文件

- `nz_coder/interface/timeline.py`、`interface/commands/handlers/core.py`、`state/sessions.py`。

### 108.6 验证结果

- timeline/CLI/Session/HTTP组合`81 passed`；选定文件Ruff通过；最终全量`1105 passed`，1条既有Python 3.13 fork warning。

### 108.7 学习重点

1. 相似UI动作不代表相同数据关系：conversation fork是独立顶层Session，task delegation才是parent/child Session。
2. 深复制不是协议克隆；只要实体ID会进入事件、恢复或引用字段，就必须重建完整引用图。

### 108.8 剩余差距

- Kilo task-child递归复制差距已由A110解决，并额外覆盖NZ独立worktree ownership。
- InfCode会用title Agent把默认标题改成首个任务摘要；NZ当前已有手动`/rename`与稳定默认标题，但尚无同构自动标题consumer。

## 109. A106：Session默认标题fallback

### 109.1 InfCode 参考能力

- `session.ts`让所有顶层Session拥有`New Session`默认标题；`prompt.ts::fallbackTitle()`从首个非synthetic User TextPart压平空白并截到100字符。
- `setTitleIfDefault()`在写入前重新检查当前标题，避免异步title Agent或fallback覆盖用户手工rename；run结束仍会执行fallback兜底。

### 109.2 NZ-Coder 原有不足

- A105以前Session标题可以完全缺失；补默认值后若没有后续消费，Session列表会永久充满`New Session`，实际产品不可辨认。

### 109.3 实现结果

- `save_session()`仅在当前标题仍为`New Session`时，从首个真实User文本生成fallback；压平空白，超过100字符时保留97字符加省略号。
- synthetic reminder、非文本附件和空消息不能生成标题；已有fork标题与`/rename`标题均保持不变。

### 109.4 关键设计决策

- 本阶段先实现InfCode自身保证执行的确定性fallback，不额外消耗一次Provider请求做title Agent；这保证离线、HTTP和CLI保存路径口径一致。
- 标题推导放在Session持久化owner中，因此运行前checkpoint和运行后checkpoint都不会漏掉，也不会创建第二套UI状态。

### 109.5 关键文件

- `nz_coder/state/sessions.py`、`tests/test_session_lifecycle.py`。

### 109.6 验证结果

- Session/CLI/HTTP组合`80 passed`；Ruff通过；最终全量`1107 passed`，1条既有Python 3.13 fork warning。

### 109.7 学习重点

1. 默认标题是数据协议的一部分；只有生成默认值而没有从默认态退出的闭环，仍不是产品能力。
2. fallback写入必须带“仍为默认值”的条件，否则后台增强会和手工rename形成竞态。

### 109.8 剩余差距

- 未调用专属title Agent生成更抽象的短标题；当前确定性fallback对应InfCode模型失败或run结束时的必达路径。

## 110. A107：子Agent统一Message/Part生命周期

### 110.1 InfCode 参考能力

- InfCode的task child仍是完整Session：Provider step由同一Processor产生Assistant、StepStart/Finish、ToolPart、usage/cost和终态，不保存一套仅供内部读取的裸SDK消息。

### 110.2 NZ-Coder 原有不足

- 顶层Agent已经完成A017–A104协议链，但自定义child loop仍直接`msg.model_dump()`并追加裸tool message；恢复、stats、fork和调试无法获得单步事实。

### 110.3 实现结果

- child User/Assistant/tool统一分配Session内Message/Part身份；Assistant绑定真实User parent、child类型、worktree/root、provider/model和请求时间。
- 每次child响应持久单步tokens/cost；SessionProcessor创建StepStart/Finish和ToolPart pending→running→completed/error状态，大输出仍引用原持久化结果。
- child原子保存前统一normalize消息图，并只给最终Assistant写completed/canceled/errored/interrupted endState。

### 110.4 关键设计决策

- 复用顶层SessionProcessor作为唯一状态机，而不是给child再写一套近似事件结构。
- `start_step`增加可选真实请求起始时间，默认接口保持兼容；child late materialization不再伪造step开始时间。

### 110.5 关键文件

- `nz_coder/runtime/subagent.py`、`runtime/session_processor.py`、`tests/test_subagent.py`。

### 110.6 验证结果

- child/manager/processor/stats/schema组合`66 passed`；Ruff通过；最终全量`1107 passed`，1条既有Python 3.13 fork warning。

### 110.7 学习重点

1. 子Agent隔离不能以牺牲协议一致性为代价；否则主Session的恢复、统计和分支能力无法递归复用。

### 110.8 剩余差距

- child state仍存放在父Session artifact下而非顶层Session表；fork child与独立worktree复制已由A110解决。
- child Provider错误与RetryPart分别由A108、A109补齐；更细的第三方SDK错误类型仍可扩展。

## 111. A108：子Agent Provider错误Assistant owner

### 111.1 InfCode 参考能力

- InfCode在Provider请求前创建Assistant owner；请求失败由同一Processor写finish/error/time，因此错误、取消与成功共享Message事件和恢复协议。

### 111.2 NZ-Coder 原有不足

- A107补齐成功响应后才创建child Assistant；Provider在返回消息前超时、取消或抛错时，仍只有child `state.status`和trace，没有可被Session consumer读取的错误消息。

### 111.3 实现结果

- child失败路径创建带parent、agent/path、provider/model和真实请求起点的Assistant，写StepStart/Finish。
- 父取消写`MessageAbortedError`与canceled，Provider失败/超时写不可重试`APIError`与errored；随后和child state在同一原子保存边界提交。

### 111.4 关键设计决策

- 错误Assistant必须由Provider边界创建，不能让加载器从status字符串猜测；只有边界知道这是Provider错误还是用户取消。

### 111.5 关键文件

- `nz_coder/runtime/subagent.py`、`tests/test_subagent.py`。

### 111.6 验证结果

- child/manager/processor/schema组合`64 passed`；Ruff通过；最终全量`1108 passed`，1条既有Python 3.13 fork warning。

### 111.7 学习重点

1. “失败也要有owner”是可恢复Agent协议的硬约束；只有日志或status无法支持统一UI、统计和分支。

### 111.8 剩余差距

- child RetryPart已由A109解决；具体SDK异常仍可继续复用顶层`assistant_error_from_exception`提高身份细度。

## 112. A109：子Agent Provider RetryPart闭环

### 112.1 InfCode 参考能力

- InfCode Processor在同一Assistant step上持久RetryPart；transient Provider错误按retryable分类与`Retry-After`安排`next`，恢复后继续同一请求owner。

### 112.2 NZ-Coder 原有不足

- A108让child失败可见，但任何连接、429或5xx异常都会立即终止；child与顶层Agent使用了不同的恢复语义。

### 112.3 实现结果

- child复用`RecoveryState`的认证/4xx/context非重试分类，以及408/409/425/429、5xx、timeout/connection transient分类。
- 首次transient错误创建唯一Assistant/StepStart，后续attempt在同一owner写typed RetryPart；遵守Retry-After或指数退避，最多三次。
- backoff使用child cancel event等待；取消会立即结算同一Assistant为MessageAbortedError，不再睡满退避时间。

### 112.4 关键设计决策

- 重试不是调用函数外层的隐藏循环：attempt/error/next必须先原子持久化，Session恢复与UI才能看到真实等待状态。
- 成功响应更新原retry owner，不能为一次逻辑step创建第二条Assistant。

### 112.5 关键文件

- `nz_coder/runtime/subagent.py`、`tests/test_subagent.py`。

### 112.6 验证结果

- child/manager/processor/schema组合`65 passed`；Ruff通过；最终全量`1109 passed`，1条既有Python 3.13 fork warning。

### 112.7 学习重点

1. 顶层和child共享Provider时必须共享错误恢复契约，否则相同模型故障会产生两套不可解释行为。

### 112.8 剩余差距

- child层级工具进度已由A112通过父task ToolPart metadata闭环；child RetryPart仍保存在child Session历史中，父卡目前只显示retrying状态而不展开typed error详情。

## 113. A110：Fork task-child递归克隆与worktree所有权

### 113.1 InfCode 参考能力

- `kilocode/session/fork.ts::remapChildren()`扫描fork后的task ToolPart，按child Session去重调用`Session.fork()`，递归复制nested child并把metadata `sessionId`改为新ID。

### 113.2 NZ-Coder 原有不足

- A105只重建父Session消息图，task ToolPart仍会引用旧父Session拥有的child state，造成恢复、事件、权限和后续修改跨fork串线。
- NZ write child比InfCode多一层独立Git/copy worktree；只复制JSON会让两个fork继续共享旧工作区，或让新child丢失尚未apply的修改。

### 113.3 实现结果

- 扫描task ToolPart的`state.metadata`/兼容metadata，对每个child只克隆一次并重写`child_session_id`或`sessionId`。
- child生成新Session/Agent/Message/Part身份，重绑parent Session/Agent并清空trace owner；若历史出现nested task引用，按同一函数递归处理。
- read-only/direct child绑定父workspace；Git/copy child创建新的受管worktree，并从旧worktree精确覆盖changed files和deleted paths，同时复制scratch，保留baseline/conflict事实。
- queued/running/cancel_requested child拒绝fork；任何克隆失败按逆序移除本次child artifacts/worktrees。CLI关闭未接管的新Agent并恢复旧Session。
- target state在覆盖任何文件前先写入新worktree ownership；故障注入证明overlay中途失败只回收新资源，不会误删deep-copy中曾携带的来源worktree metadata。

### 113.4 关键设计决策

- 普通fork仍没有parentID；这里只克隆它引用的task child ownership，两种关系继续分离。
- 不复制`.git`目录或整棵worktree；由WorktreeManager建立合法新所有权，再覆盖已声明changed paths，避免损坏Git metadata或复制无界依赖目录。
- 缺失的历史child与InfCode一样保留原引用；活动child则显式失败，因为NZ本地线程/worktree快照无法原子克隆运行中副作用。

### 113.5 关键文件

- `nz_coder/runtime/subagent.py`、`interface/commands/handlers/core.py`、`tests/test_subagent.py`、`tests/test_cli_commands.py`。

### 113.6 验证结果

- fork/child/CLI/Session/timeline组合`67 passed`；Ruff与编译检查通过；最终全量`1113 passed`，1条既有Python 3.13 fork warning。

### 113.7 学习重点

1. Session引用重写和资源所有权迁移必须是同一事务；只换ID会制造更隐蔽的共享状态。
2. 上游源码假设共享worktree时，移植到独立worktree架构必须保留语义而不是逐行照搬实现。

### 113.8 剩余差距

- 活动background child目前阻止fork；若未来需要运行中fork，必须先增加线程settle或一致性快照协议。
- 大型changed file复制仍是同步操作，但范围严格限定为child已记录的changed paths。

## 114. A111–A113：终端消息消费、child层级投影与真实PTY发布门

### 114.1 InfCode参考能力

- `cli/cmd/tui/context/sync.tsx`按message ID/part ID归一化`message.updated`和`message.part.updated`，route只消费统一状态。
- `routes/session/index.tsx::AssistantMessage`渲染typed error和Assistant agent/model/duration；`Task`从child Session消息图提取当前工具与工具计数。
- `component/prompt/index.tsx`在同一个存活prompt中维护退出按键状态，不以销毁并重建输入器实现双击退出。

### 114.2 NZ-Coder原有不足

- A094已经生产typed Assistant error/finish/endState事件，但终端未订阅，形成协议有字段、产品看不到的半闭环。
- child已有完整Message/Part历史，父task却只在结束时收到摘要；运行时用户仍看到无信息的task等待。
- 第一次空Ctrl+C让PromptSession退出、打印提示再重建；真实PTY证明第二次按键可能落进重建间隙，单元状态测试没有覆盖该时序。

### 114.3 实现结果

- `TerminalRunRenderer`订阅`message.updated`，按message ID reconcile Assistant，忽略`MessageAbortedError`错误卡，对其他typed error只渲染一次；ProviderAuth/context/output-length给出明确恢复动作。
- run终态消费最新Assistant的agent/model/time/endState，终端不再仅显示一个无身份的通用Run行。
- child复用现有execution-local tool metadata reporter，把child session/status/current tool/title/count更新进父task ToolPart；terminal用`↳`显示层级进度。
- owned composer的第一次空Ctrl+C只更新同一PromptSession内的armed状态与bottom toolbar，第二次才退出；有草稿时仍先清空。
- `scripts/release_smoke.py`在source-external wheel环境创建真实PTY，验证composer、slash补全、草稿清空、双Ctrl+C退出码0和无Traceback。

### 114.4 关键设计决策

- 不新增UI专用error或child event；Message/Part仍是唯一事实源，terminal只是consumer。
- NZ child存储在父Session artifact下，当前没有InfCode顶层Session sync表；因此把上游“读取child message store”翻译成有owner的父task metadata投影，而不是伪造第二个Session API。
- Ctrl+C竞态必须用真实PTY证明；直接测试`_empty_ctrl_c_requests_exit()`只能证明计时函数，不能证明终端输入生命周期。

### 114.5 关键文件

- `nz_coder/interface/run_renderer.py`：Assistant error/footer与task child进度consumer。
- `nz_coder/runtime/subagent.py`：child→parent task progress producer。
- `nz_coder/interface/terminal_input.py`：同一composer内的双Ctrl+C状态。
- `scripts/release_smoke.py`：已安装wheel真实PTY发布门。
- `tests/test_run_renderer.py`、`tests/test_subagent.py`、`tests/test_terminal_input.py`：协议与时序单元边界。

### 114.6 验证结果

- 定向组合：`65 passed`。
- source-external non-editable wheel与真实PTY：通过。
- 完整回归：`1118 passed`，1条既有Python 3.13 multiprocessing fork warning。
- SWE-bench与公网Provider：按用户要求未运行。

### 114.7 学习重点

1. schema/producer已经存在不等于产品对齐，必须沿事件一直审到可见consumer。
2. 子Agent实时状态应由父task拥有的Part投影，不能让UI直接读无边界的线程内部状态。
3. 键盘交互的正确性包含PromptSession生命周期和PTY时序，纯函数测试无法替代真实终端冒烟。

### 114.8 剩余差距

- 仍是scrolling REPL，不是InfCode OpenTUI全屏布局；历史卡原位展开与持久sidebar未实现；A114已提供child只读picker/transcript，但没有把child变成可直接输入的活动顶层route。
- child retry只显示retrying摘要；若要展开typed error，应把child RetryPart做成父task的有界结构化metadata，而不是读取child文件轮询。
- macOS、Python 3.9–3.12矩阵尚无CI证据；Windows适配按用户决定继续不做。

## 115. A114：task child Session只读导航

### 115.1 InfCode参考能力

- `routes/session/index.tsx`注册first/next/previous child与parent navigation；Task卡的child Session ID进入统一route。
- `routes/session/dialog-subagent.tsx`只提供明确Open动作，打开child自身的Message/Part历史。

### 115.2 NZ-Coder原有不足

- A112只能看见child当前工具；终态后没有可发现的child列表，也无法学习child如何得出结果。
- child state位于父Session artifacts且拥有独立worktree，直接复用顶层`/resume`会错误替换Agent/workspace owner。

### 115.3 实现结果

- 新增精确父Session所有权下的child list/load只读API；ID必须原样通过安全校验，返回deep copy，损坏时间字段安全降级。
- 新增`/subagents [ID]`与`/children`别名；无参数在交互终端打开picker，非交互终端输出表格，显式ID打开完整child transcript。
- transcript复用统一Message/Part formatter并显示agent/status/model/message count；父Session ID、Agent、history和worktree始终不变。

### 115.4 验证结果与边界

- CLI/subagent/terminal组合：`97 passed`；完整回归`1120 passed`，1条既有Python 3.13 fork warning。
- SWE-bench、公网Provider未运行。
- 当前是安全的read-only route翻译；child继续交互仍通过父Agent的task resume协议，尚未提供InfCode式child route内直接prompt。

### 115.5 学习重点

1. “打开child”不仅是读一个JSON，还必须尊重child worktree和Agent owner；没有可安全接管的运行时就应明确只读。
2. 产品导航必须从稳定parent ownership解析child，不能全盘扫描所有artifact后按相似ID猜测。

## 116. A115–A118：full-screen Session视图、历史详情、sidebar与interactive child

### 116.1 InfCode参考能力

- `app.tsx`由单一OpenTUI renderer拥有alternate screen、keyboard/mouse和退出恢复；`routes/session/index.tsx`用scrollbox承载Message/Part、底部固定Prompt。
- `dialog-message.tsx`从一个Message/Part集合恢复prompt或执行操作；session route支持parent/child切换。
- `sidebar.tsx`在宽终端自动显示42列、窄终端隐藏，并允许显式overlay；内容只消费已有project/session同步状态。

### 116.2 NZ-Coder原有不足

- inline PromptSession让输入框存在，但历史依赖scrollback，输入阶段没有固定transcript viewport。
- `/timeline`只有摘要，全局`/tool-details`无法只检查一个历史turn。
- child只读transcript已完成，但用户无法直接给该child追加follow-up；把child交给顶层`/resume`又会破坏worktree/tool owner。

### 116.3 实现结果

- 新增`FullscreenComposer`：alternate screen内由同一个prompt_toolkit Application管理header、200K有界transcript、PageUp/PageDown viewport、固定3–8行composer、completion float和footer。
- 默认真实CLI从统一Message/Part history重建transcript；提交后退出alternate screen执行Agent，下一次idle输入重新进入并显示最新历史。非TTY、嵌入式fake PromptSession及selector保持旧路径。
- `/message [TURN]`通过picker或编号只渲染目标turn的完整ToolPart详情。
- 42列sidebar消费Session title/id、workspace、message/context计数与ChangeTracker；`auto/show/hide`进入workspace terminal preferences。
- transcript经Rich Markdown生成prompt_toolkit formatted text；用户内容中的ANSI/control先清除，避免viewport执行终端控制。
- sidebar继续消费todo持久状态、已有MCP runtime status和workspace-scoped LSP client状态；读取UI不会启动新server。
- `/subagent [ID] [PROMPT]`只接受当前parent精确owned且非活动child，在`scoped_parent_context`下复用`run_subagent_async`和原agent type/allowed tools/claimed paths/worktree；父runtime从未被替换。

### 116.4 关键设计决策

- transcript没有复制一份UI数据库，而是每次从Session Message/Part事实重建；200K只限制屏幕投影，不删历史。
- 本阶段没有假称InfCode单一长期存活renderer完全对齐：Agent运行期间仍退出alternate screen，让现有Rich streaming/tool/permission交互工作；下一步若长期驻留，必须先统一Rich输出sink和selector overlay。
- interactive child是显式命令route，不把child伪装成顶层AgentLoop；其取消仍先settle thread/provider/tool再返回。

### 116.5 关键文件与验证

- `interface/fullscreen.py`：全屏layout owner；`terminal_input.py`：入口选择和输入合同；`cli.py`：transcript/sidebar provider。
- `interface/commands/handlers/core.py`：message/sidebar/subagent产品入口；`preferences.py`：sidebar持久状态。
- 首轮组合`96 passed`，最终新增投影组合`56 passed`，Ruff/compile通过；完整回归`1125 passed`；source-external wheel真实PTY通过。
- SWE-bench与公网Provider按用户要求未运行。

### 116.6 剩余差距

- Agent运行阶段仍使用Rich scrolling renderer，不是InfCode同一个60fps长期renderer；运行时composer不能继续输入队列，只能Ctrl+C取消。
- Markdown已进入Rich安全渲染，但还不是OpenTUI增量syntax renderer；PageUp/PageDown是viewport级滚动，尚无鼠标点选原位卡片。
- sidebar已有本地Todo/MCP/LSP组件，但没有通用第三方TUI plugin slot或折叠/点击交互。

## 117. A121：单一长期存活终端Application

### 117.1 InfCode参考能力

- 参考文件：`packages/opencode/src/cli/cmd/tui/app.tsx`、`routes/session/index.tsx`、`routes/session/permission.tsx`、`ui/dialog.tsx`、`ui/dialog-select.tsx`。
- 核心行为：OpenTUI renderer长期拥有terminal；Session消息、运行状态、composer和dialog属于同一状态树，提交prompt或弹出权限选择不会销毁根应用。

### 117.2 NZ-Coder原有不足

- A115的`FullscreenComposer.read_async()`每次提交都执行`Application.exit()`，Agent运行阶段又回到Rich `Live`，下一轮再创建新Application。
- selector和小文本输入各自启动新的full-screen/PromptSession；因此输入阶段的外观虽接近产品，生命周期仍是多套renderer拼接，存在闪屏、输入丢失和Ctrl+C竞态。

### 117.3 实现结果

- `TerminalInput`只创建一个`FullscreenComposer`；首次读取启动Application task，后续提交通过async queue返回，Application直到CLI退出才关闭。
- transcript、流式token、ToolPart卡片、retry/working状态、slash命令输出和结束状态全部投影到同一screen；Rich scrolling/inline PromptSession只保留给非TTY或注入式embedder。
- permission/question/model/session/command palette共用同一Float overlay和focus owner，不再嵌套第二个terminal Application。
- Agent运行期间composer继续可编辑并可排队下一条输入；Ctrl+C通过当前run task callback取消本轮Agent，不退出terminal根应用。
- `/theme`、`/sidebar`和`/mouse`原位热更新同一Application，不再暗中依赖下一轮重建。
- CLI正常退出后才离开alternate screen并打印最终Goodbye，避免最终状态被screen restore吞掉。

### 117.4 关键设计决策

- 没有逐行复制Solid/OpenTUI TSX；Python端保留prompt_toolkit，但复制上游最关键的生命周期不变量：一个root renderer、一个Session事实源、overlay而非嵌套app。
- `Session Message/Part`仍是durable truth；screen中的stream/status/tool卡是有界瞬态投影，run结束后由durable transcript接管，未增加UI数据库。
- `_SurfaceConsole`只做Rich renderable→ANSI formatted projection；非TTY继续使用原Rich Console，避免破坏评测和管道输出。

### 117.5 关键文件

- `nz_coder/interface/fullscreen.py`：长期Application、submission queue、run projection、selector/text overlay和终端恢复。
- `nz_coder/interface/terminal_input.py`：唯一surface owner及selector/password路由。
- `nz_coder/interface/cli.py`：surface console、stream/run task取消和退出生命周期。
- `scripts/release_smoke.py`：installed wheel真实PTY的同屏命令与单次alternate-screen断言。
- `tests/test_fullscreen.py`：连续提交和overlay不重建Application的回归。

### 117.6 验证结果

- 静态检查：相关文件Ruff与`git diff --check`通过。
- 定向测试：124项terminal/command/selector/run renderer组合通过。
- 完整测试：1128 passed，1条既有Python 3.13 multiprocessing fork warning。
- 真实冒烟：source-external wheel安装后真实PTY通过composer、slash completion、同屏`/help`、双Ctrl+C、无Traceback；原始控制流严格只有一次`CSI ?1049h`与一次`CSI ?1049l`。
- 评测：按用户要求未运行SWE-bench，也未调用付费Provider。

### 117.7 学习重点

1. “有full-screen界面”不等于terminal产品对齐；根Application是否跨prompt、run和dialog长期存活才是生命周期边界。
2. Ctrl+C必须作用于当前owner：dialog先dismiss，run中cancel Agent，idle空composer才进入退出手势。
3. 真实PTY应验证控制序列次数；只检查屏幕上出现某段文字无法发现renderer反复退出/重建。

### 117.8 剩余差距

- NZ仍使用prompt_toolkit+Rich投影，不具备OpenTUI逐组件增量布局、鼠标原位ToolPart展开和通用plugin slot。
- 当前queued input是本地submission queue，尚没有InfCode SDK/server侧prompt queue与跨客户端队列管理。
- macOS/Windows及Python 3.9–3.12真实terminal矩阵仍未验证。

## 118. A122：长会话虚拟化渲染与sticky scroll

### 118.1 InfCode参考能力

- 参考文件：`routes/session/index.tsx`中的`createMemo(messages)`、`ScrollBoxRenderable`、`stickyScroll=true`、`stickyStart="bottom"`和Message/Part独立组件。
- 核心行为：消息数据响应式更新，scrollbox只消费当前viewport；新内容默认跟随底部，用户主动上翻后不强拉，且可以显式回到底部。

### 118.2 NZ-Coder原有不足

- A121虽统一了Application，但每次token/spinner invalidate仍执行`format_transcript → Rich Markdown → split_lines`整段历史。
- 流式模型文本曾作为ANSI输入解析；历史工具/命令输出只限制block数量，没有总字符上限。
- Window默认cursor在首行，没有真实sticky-bottom owner。

### 118.3 实现结果

- durable transcript按dirty revision和内容宽度缓存Rich formatted fragments，并只分割一次逻辑行。
- 新增虚拟化`UIControl`，每帧通过line group索引提供viewport所需行；stream、status和命令/tool output使用独立缓存，不再复制/切分全部历史。
- stream先清除ANSI/C0控制字符，以普通文本显示；durable结束后再走安全Markdown。
- 单block上限40K、累计瞬态输出上限120K、最多100 blocks，裁剪在ANSI解析后按fragment边界完成。
- 默认cursor锚定最后逻辑行；PageUp/PageDown/Home脱离follow，End恢复bottom follow。

### 118.4 验证结果

- 10000行历史、1000次stream/status UIContent更新：provider调用1次，核心路径由约7.457秒降至0.0055秒。
- 新增durable cache、stream控制序列、output总边界和sticky follow测试。
- A122–A124最终纳入Python3.12/3.13各1133项完整回归。

### 118.5 剩余差距

- prompt_toolkit仍会绘制整个terminal frame，不是OpenTUI逐组件GPU/renderer diff；当前优化边界是行数据虚拟化。
- 尚无鼠标滚动条、消息边界跳转和原位ToolPart展开。

## 119. A123：Terminal ErrorBoundary与安全降级

### 119.1 InfCode参考能力

- 参考文件：`app.tsx`的Solid `ErrorBoundary`以及`component/error-component.tsx`的reset/exit和terminal destroy链。
- 核心行为：fatal renderer异常有显式owner，允许reset或恢复terminal后退出，不能留下半初始化raw mode。

### 119.2 NZ-Coder原有不足

- root Application提前结束时，`read_async()`只等待submission queue，可能永久挂起。
- full-screen失效后，CLI、StreamingRenderer和CommandContext仍持有旧surface console，简单关闭界面不能形成可用降级。

### 119.3 实现结果

- `read_async()`同时等待submission和Application task；异常、取消及正常EOF均有确定终态。
- fatal renderer第一次失败重建Application并保留draft；同一进程最多自动恢复一次，避免无限重启。
- 第二次失败恢复terminal，禁用surface projection；同一个console wrapper和renderer随即委托Rich inline输出，CLI继续可用。
- close路径结算已失败/取消task，不泄漏unretrieved task exception。
- installed PTY增加100→150→80列resize和`/help`、`/status`连续命令，仍断言单次alternate-screen enter/leave。

### 119.4 验证结果

- 新增root failure无死锁、一次恢复以及surface console fallback测试。
- 120项terminal组合、Ruff、真实source-external PTY通过；最终纳入1133项双版本完整回归。

### 119.5 剩余差距

- NZ的二次fatal使用安全inline模式，不提供InfCode式可复制issue URL/stack交互页。
- paid Provider断网/限流真实互操作仍未执行；typed APIError/retry链只有fixture证据。

## 120. A124：credential-free首装与Linux Python矩阵

### 120.1 目标产品链

- 新可信workspace必须完成wheel依赖安装、`init`、0600配置、credential-free doctor、CLI启动、本地命令、resize和安全退出。
- 发布证据不能继承开发机shell credential或site-packages后声称首装成功。

### 120.2 NZ-Coder原有不足

- release smoke使用`system_site_packages=True`且`pip install --no-deps`，可能借用开发环境依赖。
- Ubuntu系统Python3.12缺`ensurepip/python3.12-venv`时，stdlib venv直接失败。
- credential warning在alternate screen启动前打印，进入TUI后不可见；smoke又继承API key，无法发现。

### 120.3 实现结果

- release environment改为隔离site-packages并让wheel安装解析声明依赖。
- 有`ensurepip`时使用stdlib venv；发行版裁剪Python则显式回退已安装`virtualenv`，无可用builder时给确定错误。
- doctor/PTY环境移除常见Provider和视觉key，验证无credential仍能进入产品、显示`/connect`提示并执行本地命令。
- missing-credential警告改在长期surface建立后输出，不再被alternate-screen清除。

### 120.4 验证结果

- Linux Python3.12.3：source-external wheel、依赖安装、help、init 0600、credential-free JSON doctor、真实PTY全部通过；完整回归1133 passed、1条既有fork warning。
- Linux Python3.13.12：同一wheel/PTY门通过；完整回归1133 passed、1条既有fork warning。
- 未调用付费Provider、公共MCP或SWE-bench。

### 120.5 剩余差距

- Python3.9–3.11解释器当前机器不可用，尚无运行证据；macOS/Windows按现有产品范围未验证。
- 真实Provider首次连接需要用户凭据，release门只能验证credential-free降级与本地命令。

## 121. A125：full-screen leader map与复制最新回答

### 121.1 InfCode参考能力

- 参考文件：`config/keybinds.ts`的`messages_copy: <leader>y`，以及`routes/session/index.tsx`中从最后Assistant的非空TextPart拼接并写clipboard的consumer。
- 核心行为：复制回答与导出整个Session是不同命令；快捷键属于长期TUI根应用，不能只存在于旧输入组件。

### 121.2 NZ-Coder原有不足

- inline PromptSession已有Ctrl+X leader map，但A121新Application只迁移了外部编辑器和模型cycle，文档所列T/M/N/L/G/C/S/U/R在默认全屏入口失效。
- `/copy`只能复制整个Markdown transcript，没有InfCode式“复制最后一条回答”。

### 121.3 实现结果

- full-screen和inline入口共用T/M/N/L/G/C/S/U/R/Y语义；leader action继续进入同一submission queue，不退出Application。
- 新增`/copy-last`和Ctrl+X Y，从最新Assistant的typed TextPart读取，过滤ignored/synthetic和reasoning；旧Session回退content。
- 无文本、超过clipboard边界和无clipboard transport均给确定反馈；`/copy`整会话语义不变。

### 121.4 验证结果

- pipe-input验证Ctrl+X Y在同一Application返回`/copy-last` action且不重建root task。
- 新增typed Part选择和命令clipboard测试；相关119项组合通过。
- Linux Python3.12/3.13各1135项完整回归、各1条既有fork warning；两版本source-external wheel真实PTY通过。

### 121.5 剩余差距

- A126/A127已补齐消息组件级next/previous、鼠标点选详情和原位ToolPart折叠。

## 122. A126/A127：消息身份导航与ToolPart交互

### 122.1 InfCode参考能力

- 参考文件：`routes/session/index.tsx`中的`findNextVisibleMessage`、`scrollToMessage`、first/last/last-user命令、`UserMessage`点击详情，以及`GenericTool`/`BlockTool`的局部expanded状态。
- 参考配置：`config/keybinds.ts`中Home/End首尾导航；next/previous/last-user默认可由命令系统配置。
- 核心行为：滚动目标是含有效TextPart的Message组件，不是任意Markdown行；工具展开状态属于TUI projection，不写回Session协议。

### 122.2 NZ-Coder原有不足

- `format_transcript()`把整个Session投影成一个字符串，Rich渲染后只剩行，message ID和Part ID全部丢失。
- PageUp/PageDown只能按固定行数移动，`/message`需要先输入turn；鼠标无法知道点击属于哪条消息。
- hidden/compact/full只控制输出量，没有每个durable ToolPart的独立折叠状态。

### 122.3 实现结果

核心调用链变为：

```text
Session Message/Part graph
  -> build_transcript_document()
  -> TranscriptBlock(message_id, role, turn_number, part_id)
  -> 每个block独立Rich Markdown渲染
  -> MessageAnchor(start/end logical line)
  -> keyboard/slash/palette navigation 或 mouse activation
```

- `format_transcript()`仍返回兼容的完整Markdown；长期终端改读`TranscriptDocument`，没有破坏export/copy消费者。
- 只把非synthetic且有文本的User/Assistant加入消息导航；next/previous按当前scroll位置选择边界，无目标时退化为分页。
- 新增`/message-first`、`/message-last`、`/message-next`、`/message-previous`、`/message-last-user`，并提供Home、End、Ctrl+X J/K/H。
- 点击User/Assistant显示同一root Application内的详情overlay；Enter/Esc返回，不创建嵌套事件循环。
- 每个typed ToolPart使用稳定Part ID形成独立projection block。hidden不显示；compact显示状态行并可点击展开；full直接显示input/output。展开集合只存在于`FullscreenComposer`。
- 200K transcript边界按完整最新block裁剪，并显示omitted标记；不再从代码围栏或消息中间截断。

### 122.4 关键设计决策

- 没有复制InfCode的Solid/OpenTUI组件代码，因为NZ使用prompt_toolkit；复制会引入不可运行的renderer依赖。对齐的是Message/Part身份、边界导航和局部UI state所有权。
- `TranscriptDocument`是只读projection，不成为第二个Session事实源。message/part内容仍由现有schema和processor拥有。
- 鼠标使用prompt_toolkit Window映射后的logical row，因此滚动、换行和resize后仍命中重新计算的anchor。
- legacy字符串provider仍保留，保证非CLI嵌入和旧测试不被强制迁移。

### 122.5 关键文件

- `nz_coder/interface/timeline.py`：structured transcript、Message/ToolPart block和兼容Markdown导出。
- `nz_coder/interface/fullscreen.py`：block级缓存、line anchor、键盘导航、鼠标详情和Part展开状态。
- `nz_coder/interface/terminal_input.py`：持久surface导航桥。
- `nz_coder/interface/commands/handlers/core.py`：命令面板/斜杠导航入口。
- `tests/test_timeline.py`、`tests/test_fullscreen.py`：身份、Part projection、导航和激活回归。

### 122.6 验证结果

- 静态检查：相关源码和测试Ruff通过，`git diff --check`通过。
- 定向测试：63项timeline/fullscreen/terminal组合通过。
- 完整测试：Linux Python3.12与3.13各1137项通过，各保留1条既有multiprocessing fork warning。
- 真实冒烟：两版本source-external wheel执行credential-free、resize、slash/连续命令、单一alternate-screen与双Ctrl+C PTY门通过。
- 未调用付费Provider、公共MCP或SWE-bench。

### 122.7 学习重点

1. “源码级对齐”不是把TSX粘到Python，而是追踪上游状态owner和consumer：Message ID必须一直保留到renderer hit-test。
2. 虚拟化与交互必须共享同一行模型；另建鼠标坐标表会在resize/折行后漂移。
3. ToolPart展开是瞬态视图状态，持久化它反而会污染Agent协议并增加并发冲突。
4. transcript边界必须按语义块裁剪，否则Markdown围栏损坏会把历史内容误渲染成代码或控制文本。

### 122.8 剩余差距

- A128已补齐详情独立滚动、拖选误触保护和ToolPart hover；prompt_toolkit仍没有OpenTUI同构的renderer selection→clipboard API。
- A129已补齐五个消息导航action的workspace键位覆盖；完整InfCode keybind schema仍未全部开放。
- 终端仍是本地Session graph的直接consumer，不是InfCode那样通过统一server SDK驱动的薄客户端。

## 123. A128/A129：详情交互与可配置消息键位

### 123.1 InfCode参考能力

- `ui/dialog.tsx`：对话框在mouse down记录已有selection，mouse up时若用户正在选择文本则不执行dismiss；Esc也优先保留/清除selection。
- `routes/session/index.tsx`：UserMessage、InlineTool和BlockTool点击前检查`renderer.getSelection()?.getSelectedText()`；可点击工具维护hover和局部expanded状态。
- `config/keybinds.ts`：键位是带默认值和说明的配置schema；`messages_first/last/next/previous/last_user`由action名解析，不应硬编码在组件事件里。

### 123.2 NZ-Coder原有不足

- A127详情仍复用selector结果Window，没有独立cursor/scroll owner；较长Message只能看到前15行。
- mouse up直接激活命中行，拖动选择文本时可能误开详情或展开ToolPart；无hover反馈。
- 五个消息导航键写死在`_build_bindings()`，用户无法像InfCode一样禁用或覆盖，`reload_preferences()`也不能热更新key processor。

### 123.3 实现结果

- 新增`_DetailControl`和独立`_detail_window`：Markdown按实际宽度缓存分行，详情滚动与主transcript的`vertical_scroll`完全分离。
- detail状态下Up/Down、PageUp/PageDown、Home/End只操作detail viewport；打开时预生成首帧行数，立即按End也不会使用上一个详情的长度。
- transcript control记录left mouse down坐标；mouse up坐标不同即判定为拖选并拒绝activation。同点click才进入Message详情或ToolPart toggle。
- MOUSE_MOVE按现有projection anchor解析Part ID；hover变化才标记durable projection dirty，普通移动不持续重绘。
- `TerminalPreferences.keybindings`保存经过白名单和prompt_toolkit真实parser验证的workspace override；损坏配置加载时逐项忽略，命令写入时严格报错。
- `/keybind list|reset|ACTION [KEYS|none|default]`提供产品入口。键序列用空格分隔，例如`c-x j`；`none`禁用，`default`删除单项override。
- root Application使用`DynamicKeyBindings`读取当前binding owner；theme/mouse/sidebar/keybind统一由`reload_preferences()`原位更新，不退出alternate screen。
- `/keys`显示当前effective message bindings，slash completer提示可配置action。

### 123.4 关键设计决策

- prompt_toolkit没有OpenTUI renderer selection对象。本项不伪造selection文本，而是在可验证的边界上阻止drag activation；真正的跨行选择复制仍明确保留为renderer差异。
- 先开放A126真实consumer所需的五个message action，而不是一次性把InfCode全部输入编辑键复制进来。后者会与prompt_toolkit原生Emacs/Vi输入行为和终端编码产生大量冲突。
- preference内部使用排序tuple保证frozen dataclass可比较，磁盘仍投影为JSON对象/键值序列；renderer只读合并后的有效map。
- 热更新通过DynamicKeyBindings切换binding registry，不重建Buffer、Application、Session或transcript cache。

### 123.5 关键文件

- `nz_coder/interface/fullscreen.py`：detail UIControl、独立滚动、drag guard、hover和DynamicKeyBindings。
- `nz_coder/interface/preferences.py`：action白名单、默认键、严格/宽容校验和持久化。
- `nz_coder/interface/terminal_input.py`：初始binding注入、reload热更新和slash completion。
- `nz_coder/interface/commands/handlers/core.py`：`/keybind`与effective `/keys`consumer。
- `scripts/release_smoke.py`：已安装wheel内设置/reset keybind且保持单一screen的真实PTY门。

### 123.6 验证结果

- 静态检查：全包Ruff与`git diff --check`通过。
- 定向测试：90项terminal/fullscreen/preferences/command组合通过；新增drag、hover、detail scroll和运行中binding swap测试。
- 完整测试：Linux Python3.12与3.13各1141项通过，各1条既有multiprocessing fork warning。
- 真实冒烟：两版本source-external wheel执行credential-free启动、resize、`/keybind messages_next c-n`、reset、单一alternate-screen和双Ctrl+C PTY门。
- 未调用付费Provider、公共MCP或SWE-bench。

### 123.7 学习重点

1. 对话框“能显示长文本”和“有独立scroll owner”不是同一件事；cursor必须属于详情Window，不能借主transcript滚动。
2. selection-safe click的关键是区分press/release gesture，而不是在业务handler里猜用户是否想点击。
3. 可配置键位必须先经过目标renderer自己的parser验证；只用正则会接受`a-x`等prompt_toolkit实际不支持的伪键。
4. 热更新键位不应重建root Application，否则会重现A121已经消除的alternate-screen、draft和Ctrl+C竞态。

### 123.8 剩余差距

- prompt_toolkit transcript仍不提供与OpenTUI相同的跨行selection对象、自动copy-on-select和右键copy控制。
- 当前只开放五个消息导航action；完整leader、输入编辑、model/agent/variant等键位仍使用产品默认值。
- 键位配置属于`.nz-coder/terminal/preferences.json`，尚未并入InfCode式统一config schema/watch事件。

## 124. A130：queued follow-up 步骤边界接管

### 124.1 InfCode 参考能力

- `packages/opencode/src/kilocode/session/prompt-queue.ts`：以`latest`和`activeSince`区分当前slot与后来入队的prompt，`hasFollowup()`只报告当前运行开始后到达的新请求。
- `packages/opencode/src/session/prompt.ts`：当前`handle.process`完全排空token与inline tool后检查`hasFollowup()`，以`interrupted`结束旧turn，不再开始下一次LLM round-trip。
- `packages/opencode/test/kilocode/session-prompt-queue.test.ts`与`prompt-dismiss-contract.test.ts`：覆盖follow-up时序和“不能中断当前stream、只能在step之间接管”的合同。

### 124.2 NZ-Coder 原有不足

- A121已经允许用户在Agent运行时继续输入并把请求放入full-screen queue，但AgentLoop不知道队列状态。
- 如果旧任务连续调用工具，已入队的新意图必须等旧任务自然结束或耗尽全部step，造成可见延迟和额外Provider费用。

### 124.3 实现结果

- `FullscreenComposer.has_pending_submission()`只读现有async queue；`TerminalInput`投影该状态，CLI把检查函数注入当前Agent owner。
- AgentLoop在每个已完成step后的下一轮入口检查队列。命中时以`interrupted`收束旧turn，持久化Assistant endState、runtime state和Session事件；不会发出下一次Provider请求。
- 检查发生在当前Provider stream、inline/local tool、文件事务、StepFinish与checkpoint全部完成之后，因此不截断token、不留下pending ToolPart，也不回滚已经成功的当前步骤。
- host回调异常只记录trace并按“无follow-up”降级，UI故障不能使Agent失败。
- content-addressed step start/finish snapshot相同时直接记录`workspace_patch_unchanged`，不再为确定无写入的read step重算整个Session空diff。真实大仓中的follow-up接管等待由约8.03秒降到580.7ms。

### 124.4 关键设计决策

- 复制的是InfCode的时序合同，不复制Effect/TypeScript队列实现。NZ已有一个长期prompt_toolkit Application和`asyncio.Queue`，继续由终端拥有输入队列，Agent只消费一个只读predicate。
- 不在Provider请求或工具执行中途强制cancel；那会破坏A084 stream tool settlement与A104消息终态。
- 不把排队内容提前加入当前history，因此下一次Provider请求绝不会混入尚未取得执行权的User prompt。

### 124.5 关键文件

- `nz_coder/interface/fullscreen.py`：队列待处理状态producer。
- `nz_coder/interface/terminal_input.py`、`nz_coder/interface/cli.py`：host状态投影与Agent owner绑定。
- `nz_coder/runtime/loop.py`：已结算step之间的接管检查与`interrupted`终态。
- `tests/test_loop_fake.py`、`tests/test_fullscreen.py`：Provider调用数、工具结算、endState、相同snapshot fast path与队列状态回归。

### 124.6 验证结果

- 静态检查：修改文件Ruff、compileall与`git diff --check`通过。
- 定向测试：新增3项；Loop/fullscreen/terminal interaction/command组合通过，相同snapshot不进入changed-files/session-summary producer。
- 完整测试：Linux Python3.12与3.13各1144项通过，各1条既有multiprocessing fork warning；Python3.12曾出现1次既有10ms并发观测抖动，目标用例随后连续10次及完整重跑均通过。
- 真实冒烟：两版本source-external non-editable wheel完成credential-free启动、slash、resize、命令、keybind热更新、单一alternate-screen与双Ctrl+C PTY门。
- 真实产品：直接启动`nz-coder`连接已配置DeepSeek，实际展开slash、mode/model overlay；运行`read_file`任务1秒后从同一composer提交follow-up。trace证明旧run只有1次`llm_request`、tool completed、`prompt_followup_detected`、Assistant `interrupted`，新run返回`FOLLOWUP_REAL_OK_2`，退出码0且无Traceback。
- 本次调用了已配置DeepSeek做上述最小真实链；未调用公共MCP或SWE-bench，评测流程继续按用户要求延期。

### 124.7 学习重点

1. “输入可排队”与“运行时会让新意图接管”是两种不同能力；前者没有Agent step consumer时仍可能等待几十轮。
2. follow-up不能用cancel模拟。正确边界是当前stream和工具副作用完全结算之后、下一次Provider请求之前。
3. UI队列属于host，Agent Core只依赖可注入predicate，才能保持HTTP、SWE-bench与子Agent调用方不受终端实现污染。

### 124.8 剩余差距

- NZ终端queued item在旧turn结束后才写入Session，进程异常退出会丢失尚未消费的瞬态输入；InfCode会先持久化并用message scope隔离。
- InfCode还会在follow-up到达时dismiss pending Question/Suggestion。NZ终端overlay当前独占输入并以Esc/Ctrl+C取消，没有第二个Suggestion产品面，因此未伪造该链。
- HTTP Session service继续使用显式busy/workspace gate，不复用终端瞬态queue；如未来客户端需要并发prompt提交，应单独实现持久队列和取消合同。

## 125. A131–A133：InfCodeX Agent Core 在途协作、停止协议与结果谱系

> 2026-08-28 范围审计纠偏：A131 中针对本机损坏 Python `.pth` 的 traceback 过滤不属于
> InfCodeX 产品能力，而且会把真实解释器环境损坏误记为有效验证。该生产分支和对应“忽略警告”测试已
> 回退；`.pth` traceback 现在按普通失败证据处理。下文保留当时历史经过与测试快照，不再代表当前合同。

### 125.1 InfCodeX 参考能力

本轮改用官方开源 InfCodeX 快照 `d3a81237` 作为 Agent Core 行为规格，重点阅读：

- `packages/agent/src/primitives/runner.ts`：natural-end stop hook、`reanimateCount/reanimateBudget`、abort和逐轮状态边界；
- `packages/agent/src/primitives/runner-handoff.ts`：Agent切换、终止信号和handoff span；
- `packages/agent/src/primitives/session.ts`及session-lineage实现：运行记录不是一段最终字符串，而是有parent/child、trace和结果事实的持久事件流。

### 125.2 NZ-Coder 原有不足

- 子Agent只能用`message_parent`暂停并等待父Agent回复，不能在继续工作的同时通知兄弟Agent或worker；并行任务发现冲突后只能等最终结果，协作延迟较高。
- no-tool自然结束只有旧verification/config hook字符串状态，没有InfCodeX式明确的snapshot、继续预算和abort结果；外部策略难以安全要求“再验证一轮”。
- child最终文本虽然显示session、worktree和changed files，但关键事实没有作为Tool metadata进入父Agent的`RunEvidence`。尤其是child worktree尚未apply时，不能准确区分“child改过”和“父workspace已经修改”。

### 125.3 实现结果

- `BackgroundAgentManager`新增Session-owned有界mailbox。发送目标可为精确session、display name、`worker/parent`或`*`；单条最多4000字符、broadcast最多20个live recipient、mailbox最多200条、转发链最多8层，并通过`seen_by`拒绝消息环。
- child与主Agent只在下一次Provider请求前的settled boundary消费消息。消息以synthetic user context注入并明确标为untrusted；不会中断正在进行的stream、工具副作用或事务。
- `AgentHooks`新增`StopHookContext`和`StopHookDecision`。hook可返回`None`接受结束、字符串/`reanimate`继续一轮，或`abort`显式终止；默认最多reanimate两次，每次独立run重置计数，传给hook的是deep-copy隔离快照。
- child ToolOutput metadata新增parent session、child session/agent/trace、status、changed files、conflicts和verification。`RunEvidence.child_outcomes`持久保留谱系；同步task完成只记录child事实，`apply_agent_changes`成功后才把路径归入父run的modified/output evidence。
- 组合回归发现本机损坏的Python namespace `.pth`会打印启动Traceback但保持退出码0；验证输出现在只剔除严格包围在`Error processing line ... .pth`与`Remainder of file ignored`之间的启动警告，后续真实Traceback仍会判失败，避免错误的`completed_unverified`。

### 125.4 关键设计决策

1. 没有逐行移植TypeScript Runner。NZ已有Python AgentLoop、ContextVar manager和ToolOutput metadata，复用现有owner才能保持CLI、HTTP和SWE入口一致。
2. peer message不是抢占信号。它必须等step结算后消费，否则会重新引入A084/A130已经解决的半截ToolPart和事务竞态。
3. reanimate必须有硬预算。无限stop hook比普通max-turn更隐蔽，会在模型反复声称完成时持续产生费用。
4. child changed files在apply前不能污染父RunEvidence；lineage事实与workspace事实分开保存，面试或评测审计时才能解释patch真正来自哪里。

### 125.5 关键文件

- `nz_coder/runtime/agent_manager.py`：mailbox、目标解析、转发环/容量限制及apply结果metadata。
- `nz_coder/runtime/subagent.py`：`send_message`工具、peer消息消费、child lineage/outcome producer。
- `nz_coder/runtime/hooks.py`：typed stop-hook、结果归一化、reanimate预算和run级重置。
- `nz_coder/runtime/loop.py`：worker消息settled-boundary consumer、stop reason终态、RunEvidence metadata传递。
- `nz_coder/run_evidence.py`：child outcome独立证据集合与apply后父路径归并。

### 125.6 验证结果

- 241项`verification_planner/verification/loop_fake/run_evidence/hooks/agent_manager/subagent`聚焦回归通过。
- 修改文件Ruff检查通过。
- Python启动时仍会打印开发环境既有的损坏matplotlib namespace `.pth`警告；它不影响测试结果，也不是本轮代码引入。
- 未运行SWE-bench、付费Provider或公网MCP，符合当前先做源码能力对齐的顺序。

### 125.7 学习重点

1. 多Agent能力不只是“能spawn”。中途信息路由、循环防护、消息消费边界和最终结果谱系共同决定并行是否可靠。
2. stop hook不是传统after hook；它改变Runner是否继续，必须有typed outcome、预算、trace和独立run重置。
3. child完成与child patch已进入父workspace是两个状态。若证据模型不区分，验证门会错误地把隔离worktree结果当成最终patch。

### 125.8 剩余差距

- 尚未复制InfCodeX完整的声明式Agent handoff图、inputFilter和`onAgentSwitched`；NZ当前仍以task child和显式parent resume为主。
- mailbox是进程内Session owner，child状态可持久化但未消费消息不会跨进程恢复；若HTTP演化为多进程服务，需要持久队列或事件存储。
- child内部尚未使用完整`RunEvidence`采集器，verification当前保存为有界摘要；还没有InfCodeX session-lineage reducer、artifact引用和compaction fingerprint。
- 本轮测试证明协议与证据链正确，不证明SWE-bench分数与InfCodeX相同。

## 126. A134–A136：InfCodeX声明式Handoff、持久Lineage与角色工具边界

### 126.1 InfCodeX参考能力

- `packages/agent/src/primitives/agent.ts`：Agent是包含name、instructions、tools和handoffs的声明式数据，而不是散落在Runner里的条件分支。
- `packages/agent/src/primitives/runner-handoff.ts`与`runner.ts`：只接受当前Agent声明的target；工具批次完成后应用inputFilter并替换system role，发出switch hook；无outgoing edge的角色可用terminal tool signal直接完成。
- `packages/agent/src/session-lineage/*`：Session使用append-only entry、parent关系、artifact/outcome和恢复语义；memory outcome digest与review receipt按review key去重。

### 126.2 NZ-Coder原有不足

- A131前只有parent/child任务，不存在同一Runner内从Scout切换到Worker/Evaluator的声明式角色所有权。
- 即便手工修改system prompt，所有全局工具仍暴露给每个角色，read-only scout可以越权写文件。
- handoff只进入trace会在进程退出后丢失，无法判断崩溃前已切换到哪个角色；自动记忆提取也没有可审计的review receipt。

### 126.3 实现结果

- 新增`AgentSpec`、`HandoffSpec`和`AgentGraph`：启动时验证空名称、未知target、重复edge、非continuation kind和全图cycle；失败时不进入Provider调用。
- handoff graph以execution-local动态`emit_handoff`工具暴露。工具只允许当前节点声明的edge；terminal只允许没有outgoing handoff的角色。
- AgentLoop在完整tool result、事务commit/rollback之后消费第一个合法signal；随后持久HandoffPart、应用deep-copy inputFilter、替换下一轮system role、发布`agent.handoff`和调用sync/async`on_agent_switched`。
- Agent角色可声明`allowed_tools`。Provider每轮只看到该角色工具和`emit_handoff`，dispatch前再做独立fail-closed检查，防止伪造tool call绕过schema。
- 新增0600 append-only`lineage.jsonl`，每条包含ID、session、sequence、parent ID、timestamp、type和有界payload。读取时拒绝损坏中段/断链，只容忍进程崩溃留下的最后一条非换行截断记录。
- lineage记录run started/finished、handoff、terminal和child outcome。若最后一次run没有finished，新AgentLoop从最后handoff target恢复；正常完成的下一run仍从graph start开始。
- 自动记忆流水线把outcome digest、review receipt和有保存内容时的client notice按message-ID派生review key幂等写入lineage，重复finalize不会重复提案。
- 成功的文件工具、验证命令和附件结果形成幂等artifact ledger，保存tool/action/path或command/status及附件MIME/filename，为后续compaction recovery seed提供有provenance的数据源。
- 普通回合不注入lineage；仅当history包含真实CompactionPart时，从近期handoff、child outcome、artifact和memory notice生成最多6000字符的`lineage-recovery`动态块，并明确其是provenance而非用户指令。

### 126.4关键设计决策

1. handoff发生在事务结算后，inputFilter不能影响本批工具是否提交；这与InfCodeX“tool results先进入transcript，再切换owner”的顺序一致。
2. Provider schema过滤不是安全边界，因此dispatch仍需第二层role guardrail。
3. JSONL lineage不替代现有Session messages和trace：messages负责模型上下文，trace负责性能诊断，lineage只负责有序、可恢复的业务事实。
4. 当前只实现continuation。InfCodeX的`as-tool`应复用已有隔离子Agent，而不是在主Runner内伪造同步函数调用。

### 126.5验证结果

- 169项handoff、context、memory、Loop、MessageSchema、SessionProcessor和RunEvidence组合通过。
- 除已确认受本机损坏matplotlib `.pth`原始stderr污染的`test_bash_reports_live_output_and_returns_final_metadata`外，全量1163项通过；该用例单独失败不是Agent Core回归，验证成功/失败判定已在A131轮次安全剔除此类严格包围的启动警告。
- Ruff通过；新增测试覆盖未知target、重复edge、cycle、非法terminal、prompt真实切换、Tool schema变化、inputFilter隔离、HandoffPart、终止、0600 journal、截断恢复、坏链拒绝、memory幂等和dispatch越权阻断。
- 未运行SWE-bench或付费Provider。

### 126.6后续进展与剩余差距

- 当时未实现的`as-tool`、每Agent reasoning/provider/model切换和input/output/tool guardrail，现已由A137–A140补齐核心运行链。
- lineage当前是单Session线性链，不包含InfCodeX fork/rewind/activeEntry tree、compaction anchor和artifact ledger reducer。
- 默认NZ终端尚未配置Scout→Worker→Evaluator graph；本轮交付的是可运行核心substrate，不能把“核心支持”写成“默认产品已经使用多角色链”。
- 多进程同时追加同一Session还需要OS级file lock；当前owner合同仍是单进程Session。

## 127. A137：InfCodeX `as-tool`临时Agent调用

### 127.1源码差距与实现

InfCodeX `Handoff.kind`区分`continuation`和`as-tool`：前者永久转移Runner所有权，后者只把生成的输入交给目标Agent，完成后控制权返回调用者。A134最初只接受continuation，因此仍缺一半协议。

本轮允许AgentGraph声明`as-tool`edge。调用发生时：

1. 当前tool batch与事务先完整结算，并持久HandoffPart；
2. 调用者transcript压入最多8层的run-local stack，目标Agent只收到`agent-task`委派输入；
3. 目标Agent使用自己的instructions和allowed tools运行；
4. 目标产生普通自然结束或terminal tool signal时，恢复调用者transcript/system role；
5. 目标摘要以`agent-result` synthetic消息返回，明确标为untrusted并要求根据仓库证据验证；
6. switch hook、Session event和lineage同时记录`as-tool`与`as-tool-return`。

### 127.2验证与边界

- 真实Fake Provider执行Caller→Helper→Caller三次请求，断言每次system prompt、可见transcript、返回结果和switch顺序；handoff/context/memory/Loop/MessageSchema/SessionProcessor/RuntimeContext组合180项通过；排除既有损坏`.pth`原始输出断言后的全量1164项通过。
- A137交付时stack还是run-local；A138已增加Caller frame原子持久化和崩溃恢复。多进程同时写同一Session仍未对齐。
- A139已支持as-tool目标切换Provider/model/effort；结构化output schema仍未实现。

## 128. A138：`as-tool` Caller frame原子持久化与恢复

### 128.1 InfCodeX参考与原有缺口

A137已经实现Caller→Helper→Caller，但Caller transcript只存在Python内存中。进程若在Helper执行期间退出，append-only lineage只能恢复“当前是Helper”，不能恢复Helper结束后应回到哪里以及Caller看过哪些消息。

### 128.2 实现结果

- 新增Session私有`agent-call-stack.json`，使用同目录临时文件、`fsync`与`os.replace`原子替换，权限为0600，最大8层/16 MiB。
- lineage reducer同时恢复active Agent和`as-tool`深度。启动时以lineage作为提交顺序真值：stack短于journal时拒绝不安全恢复，stack长于journal时截断未提交或返回后未清理的旧frame。
- 进入`as-tool`时先保存Caller frame再追加handoff事实；返回时先追加`as-tool-return`事实再清理frame。两个文件无法形成数据库事务，因此恢复时显式处理两个可能的崩溃窗口。
- 正常run终止后清空stack；模拟旧Agent不调用finalize、创建同Session新Agent的测试证明Helper能恢复并将结果返回Caller。

### 128.3 剩余边界

- 这是单进程Session owner下的崩溃恢复，不是多进程并发写协议；lineage和stack尚无OS级跨进程锁。
- frame是完整有界transcript快照，不是InfCodeX更完整的fork/active-entry引用图。

## 129. A139：每Agent模型运行时与reasoning升级

### 129.1 InfCodeX参考能力

`packages/agent/src/primitives/agent.ts`的Agent声明同时包含`model`、`provider`、`effort`和`AgentReasoningProfile {default,max,escalateOnRevise}`；Runner的当前Agent变化必须改变真实LLM调用，而不只是显示标签。

### 129.2 实现结果

- `AgentSpec`增加provider/model/effort/reasoning；激活角色时切换对应Provider/client缓存、registry wire model、ModelCapabilities、context/output预算、variant请求参数与model-family prompt。
- 同Provider下Caller `gpt-4o`→Helper `gpt-5/medium`→Caller的三次Fake Provider请求逐次断言真实`model`和`reasoning_effort`。
- reasoning profile支持`minimal/shallow/balanced/deep`到`low/low/medium/high`的映射；显式effort不受模型支持时fail-fast，profile在不支持reasoning的模型上保守降级。
- replan或reflection verdict要求返工时，启用`escalate_on_revise`的当前角色只升级一次到max，后续请求真实携带升级后的variant。

### 129.3 剩余边界

- 已实现跨Provider构造与切换，但本轮没有公网凭据，因此没有声称Anthropic/Gemini之间的live handoff已验证；已有各Provider adapter测试仍是离线协议证据。
- reasoning升级状态是run-scoped；进程崩溃恢复Helper时会回到其声明default，没有把临时升级另做durable事实。

## 130. A140：三类Agent Guardrail运行时

### 130.1 InfCodeX参考能力

`packages/agent/src/primitives/guardrail.ts`定义input、output、tool-before/tool-after四个触发点以及`allow/rewrite/block/escalate`四种判定。input/output属于入口Agent的run-scoped合同；tool hook每次收到handoff后的当前Agent。

### 130.2 实现结果

- 新增typed `InputGuardrail`、`OutputGuardrail`、`ToolGuardrail`及blocked/escalate异常；callback可同步或异步，verdict和rewrite payload均严格验证。
- input在首次Provider请求前运行并把改写结果作为Session/模型共同真值；output在自然终止持久化前运行。
- tool-before可改写参数、阻断执行或升级给host；阻断形成明确失败tool result并继续循环，让模型能够换方案。为保持NZ事务分类安全，guardrail不能把工具名从read改成write，只允许改参数。
- tool-after可改写/屏蔽真实结果；若把成功写操作改判失败，现有事务owner会回滚。
- output guardrail存在时正文不直接向终端流出，最终只发布审核后的内容，避免UI原文与durable Session分叉。

### 130.3 验证结果与边界

- 新增测试覆盖sync input、async output、tool block后模型第二轮自纠、当前Agent上下文、真实reasoning request、0600 stack与崩溃恢复。
- Ruff通过；核心组合186项通过；排除已确认由本机损坏matplotlib `.pth`污染原始bash输出的单项后，完整回归1170项通过。未运行SWE-bench与付费Provider。
- 当前guardrail来自入口Agent声明，尚未增加Runner调用方额外guardrail列表；这与现有AgentLoop API边界有关，后续有SDK consumer时再扩展构造参数。

## 131. A141：统一Runtime Composition与终端信号闭环

### 131.1 发现的系统性问题

A134–A140的AgentGraph、as-tool、每角色模型和guardrail已有完整单元/集成测试，但真实CLI、HTTP与三个评测入口仍各自直接构造`AgentLoop`，因此这些能力没有统一composition owner。直接把Scout→Worker→Evaluator塞进默认入口又会与NZ已有planning/replan/reflection/verification重复，形成两个控制面。

同时，显式terminal tool虽然能结束run，summary只存在tool result metadata中：non-stream终端可能看不到最终说明，output guardrail也无法审核该summary。这说明“协议模块通过”不等于“产品系统闭环”。

### 131.2 实现结果

- 新增`AgentRuntimeAssembly`。默认`coding` profile明确保留成熟native coding loop作为唯一控制面；`declared` profile才安装AgentGraph。coding+graph混装、declared无graph均在Provider调用前失败。
- CLI、HTTP Session、local eval、内置benchmark、Aider helper、SWE first-pass/retry全部改用顶层`build_coding_agent`，不再各自决定Agent构造细节；测试仍可通过lazy public AgentLoop替换注入fake。
- composition choice写入对象、`run_start` trace、append-only lineage和result runtime summary，字段为`profile/control_plane/active_agent`，评测与产品诊断可证明实际使用哪条控制链。
- terminal summary进入lineage terminal payload和最终输出；发布前经过同一output guardrail。空summary有明确fallback，不再产生“状态completed但界面空白”。
- `docs/architecture.md`同步说明composition、handoff、guardrail、lineage边界以及为什么默认产品不盲目启用多角色图。

### 131.3 关键设计决策

1. 借鉴多个仓库时，默认只允许一个orchestration owner；能力应进入共同Session/tool/transaction runtime，而不是另建平行Agent框架。
2. 默认单Worker不是功能倒退：NZ已有成熟规划、验证、reflection和child task链；在没有对照评测前叠加固定多角色只会增加token和失败面。
3. 多角色仍是正式能力，但必须显式profile选择，适合SDK、自定义workflow或后续有证据的评测配置。
4. terminal属于输出生命周期，必须经过guardrail、可见输出和lineage，而不能停在tool metadata。

### 131.4 验证与剩余边界

- 新增composition合同测试覆盖真实coding run、declared authority和混装拒绝；入口/core组合107项通过；排除既有损坏matplotlib `.pth`污染用例后的完整回归1173项通过；Ruff通过。
- 跨Provider live handoff、多进程lineage writer和声明式structured output仍未对齐。
- 本轮不运行SWE-bench；统一SWE构造入口不代表榜单分数已提升。

## 132. A142：不可信Agent准入与运行时Invariant Session

### 132.1 InfCodeX参考能力

- `packages/agent/src/admission/admission.ts`：外部或LLM生成的Agent先经过system cap和invariant审计，成功结果才携带可执行handle；内建/手写Agent保留可信快速路径。
- `packages/coding/src/agent-runtime/invariants/tool-permission.ts`：工具先映射为`read/edit/bash:*/subagent`能力，准入时裁剪声明，运行时再与parent capability scope取交集；未知工具按最严格能力处理。
- `packages/agent/src/admission/admission-session.ts`与`primitives/runner.ts`：每run创建observe/assertTerminal状态机，记录tool、handoff、mutation和evidence；`maxIterations`与调用方上限采用min-wins。
- `packages/agent/src/admission/invariants/final-owner.ts`、`evidence-trail.ts`：handoff图必须存在最终owner；发生mutation的deliverable必须携带验证证据。

### 132.2 NZ-Coder原有不足

A134–A141已经有Agent DAG、角色工具allowlist、guardrail和统一composition，但`build_declared_agent(graph)`默认信任声明本身。外部graph可以省略工具allowlist，也没有高于角色声明的host capability ceiling；`bash`一旦允许，静态声明无法区分`git status`、测试、写命令与网络命令。轮次预算、mutation/evidence和终态owner也没有绑定在一次准入会话中。

### 132.3 实现结果

- 新增`SystemAgentCap`、`AdmissionVerdict`和`AdmittedAgentHandle`。不可信Agent必须显式声明工具列表；handoff要求`subagent`能力；准入复制并裁剪新graph，不修改来源声明。
- capability采用`read/edit/bash:read-only/bash:test/bash:mutating/bash:network/subagent`。`bash:mutating`只蕴含test/read-only，不自动蕴含network；未知及未建模工具fail-closed到`subagent`。
- 新增`admitted_runtime/build_admitted_agent`，只接收成功handle，并验证handle拥有同一个已裁剪graph。原`coding`和手写`declared`入口保持可信语义，避免把第三方准入策略误加到默认CLI。
- Provider可见schema先受已裁剪角色allowlist限制；dispatch前再按具体Bash参数分类并应用system ceiling。被拒调用形成模型可见的`[Invariant toolPermission]`结果，允许下一轮改用合法方案，命令不会执行。
- `max_iterations`与用户/runtime轮次上限取最小值。准入事实、绑定invariant、裁剪说明及有效capability进入trace、lineage和runtime summary。
- 每run建立`AdmissionInvariantSession`。成功验证工具立即形成evidence；写操作只有在整个事务提交后才计入mutation，rollback不制造虚假修改事实。自然完成时若当前角色仍有outgoing handoff，或已提交修改却没有成功验证artifact，结果改判`blocked`，违规持久化为`invariant_violation` lineage。

### 132.4 关键设计决策

1. 不把InfCodeX TypeScript对象逐行翻译，而是复用NZ已有`AgentGraph`、事务、verification planner、ToolExecutionResult和lineage owner，避免出现第二套工具执行或证据系统。
2. 声明裁剪与执行检查必须同时存在：前者减少模型误选，后者防住tool guardrail重写参数和通用Bash在运行时升级能力。
3. admission只处理不可信来源。默认终端仍使用成熟native coding loop；显式手写graph仍可由SDK作者自行负责，只有外部/模型生成graph进入严格handle路径。
4. mutation在commit之后观察。若在单工具返回时记录，随后同批次失败rollback会让evidenceTrail误判工作区发生了修改。

### 132.5 关键文件

- `nz_coder/runtime/admission.py`：capability分类、准入裁剪、typed handle与run-scoped invariant session。
- `nz_coder/runtime/composition.py`：可信declared与admitted入口分离。
- `nz_coder/runtime/loop.py`：轮次min-wins、运行时二次能力检查、commit后mutation观察和终态裁决。
- `nz_coder/runtime/lineage.py`：持久`invariant_violation`事实类型。
- `tests/test_agent_admission.py`：准入、动态Bash、终态owner、证据与预算合同。

### 132.6 验证结果

- Ruff与Python编译检查通过。
- admission/runtime/handoff/composition聚焦33项通过。
- 排除已确认由本机损坏matplotlib `.pth`污染原始Bash输出的单项后，完整回归1186项通过，1项deselect，1条multiprocessing fork弃用警告。
- 未运行SWE-bench、付费Provider或真实网络命令；网络阻断测试使用不会被执行的`.invalid`地址。

### 132.7 学习重点

1. Agent-as-data只有经过host policy收窄后才是可执行对象；schema合法不等于运行权限合法。
2. capability不是工具名别名。特别是Bash，必须在调用参数落定后重新分类。
3. invariant的observe时点决定事实质量：事务前观察的是尝试，commit后观察的才是交付mutation。
4. 对齐多个仓库的正确方式是把语义接进单一owner，而不是把它们各自的Runner并排放进项目。

### 132.8 剩余差距

- 尚未实现InfCodeX完整可注册`QualityInvariant`插件表、warn/clamp/reject通用dispatcher、budget金额控制和independentReview/harnessSelectionTiming；本轮只实现对当前NZ主链有真实consumer的硬约束。
- Python typed handle主要是构造边界，不是安全沙箱；不可信代码仍不能与NZ进程共权运行。
- 跨Provider live handoff、多进程lineage writer和声明式structured output仍未对齐。

## 133. A143：Agent Structured Output与一次无工具Repair

### 133.1 InfCodeX参考能力

- `packages/coding/src/workflows/structured-output.ts`：从最后一个fenced JSON块提取候选，递归验证`type/enum/required/properties/items/additionalProperties:false`，并在声明阶段拒绝未实现但会改变约束语义的Schema关键字。
- `packages/coding/src/child-executor.ts`的`resolveChildStructuredOutput`：child真正完成后先解析；硬失败时继承原transcript，使用专用system prompt、空工具列表和`maxIter:1`进行一次repair；repair失败不改变child原终态。
- `packages/agent/src/primitives/agent.ts`与coding workflow result：`outputSchema`属于Agent/child声明，validated value单独放在`structured`字段，不把对象字段伪装成顶层result属性。

### 133.2 NZ-Coder原有不足

`AgentSpec`只能约束instructions、tools、handoffs、model和guardrail。需要机器可读结果的reviewer/helper只能返回自由文本；caller必须自己猜测JSON边界，且Provider若漏字段或多输出说明，没有统一修复和验证。直接使用各Provider的`response_format`又会拆出OpenAI/Anthropic/Gemini多条行为不一致的路径。

### 133.3 实现结果

- `AgentSpec`新增末尾可选`output_schema`字段，保持原有位置参数兼容。`AgentGraph`构造时验证Schema，并要求该角色是无outgoing handoff的terminal owner，避免声明了输出合同却在中间handoff时无人消费。
- 新增标准库validator：支持InfCodeX真实使用的Schema子集，正确区分Python`bool`与number/integer，递归报告嵌套required、enum、array item和额外字段错误。
- 声明时拒绝`$ref/oneOf/pattern/minimum`等当前validator不能兑现的约束，以及schema形式的`additionalProperties`；不会把静默忽略冒充验证成功。
- 激活角色时把稳定output instruction附到该Agent system prompt。终态优先读取最后一个JSON fence，回退到首个可解码object/array；仅schema-valid value写入Assistant metadata和最终`result["structured"]`。
- 首次失败追加synthetic repair message；下一请求使用专用repair system prompt、继承完整transcript且`tools=[]`。每角色每run最多一次，第二次仍失败只记录trace/runtime错误，不发布未验证对象。
- 显式terminal signal同样先验证summary；非法signal不结算run而进入repair。as-tool callee的validated value随恢复后的synthetic caller message携带，parent无需重新解析自由文本。
- structured结果状态进入trace、`run_finished` lineage和runtime summary；完整对象不写入普通trace，避免无界观测数据。

### 133.4 关键设计决策

1. 沿用InfCodeX validate-and-repair，而不是强制Provider-native structured output。这让所有Provider共用一条Session/repair语义，也不触碰成熟tool-call解析器。
2. repair使用原transcript但禁止工具。它只负责把已有结论重新格式化，不能借“格式修复”继续搜索、修改文件或消耗新的Agent轮次链。
3. 未验证candidate不进入`structured`。InfCodeX实现会best-effort返回parseable value，但其注释和上层合同强调schema-validated对象；NZ选择更严格的consumer边界，避免调用者误信缺字段对象。
4. output schema限制在terminal owner，是NZ对现有handoff协议的显式收敛。以后若需要中间角色typed handoff，应新增独立handoff payload schema，而不是复用终态合同。

### 133.5 关键文件

- `nz_coder/runtime/structured_output.py`：提取、Schema声明审计、递归验证和repair prompt。
- `nz_coder/runtime/handoffs.py`：AgentSpec声明与terminal-owner约束。
- `nz_coder/runtime/loop.py`：角色prompt、一次repair、空工具请求、Session/result/as-tool投影和观测。
- `tests/test_structured_output.py`：声明、嵌套校验、repair、invalid candidate、terminal signal和as-tool链路。

### 133.6 验证结果

- structured/handoff/composition/admission聚焦42项通过；5个使用`AgentLoop.__new__()`的低层兼容测试暴露惰性字段问题后已修复，对应14项复测通过。
- Ruff、Python编译与diff检查通过。
- 排除既有损坏matplotlib `.pth`污染用例后，完整回归1195项通过，1项deselect，1条multiprocessing fork弃用警告。
- 未运行SWE-bench、付费Provider或真实网络请求。

### 133.7 学习重点

1. structured output是consumer contract，不只是“提示模型输出JSON”；必须包含声明审计、提取、验证、修复上限和最终投影。
2. Provider-native JSON mode与Agent workflow structured result不是同一层，强行耦合会让工具调用和多Provider适配复杂化。
3. 一次repair必须在代码中真正限制工具和次数；只在prompt里写“不要调用工具”不构成边界。
4. 新状态字段要兼容项目里绕过构造器的底层测试替身，读取侧使用惰性默认值比要求所有测试补初始化更稳健。

### 133.8 剩余差距

- 当前只接入declared AgentGraph，不扩展旧`task`/background-agent参数schema；两条子Agent体系尚未统一成同一个typed result API。
- 未实现更完整JSON Schema、流式增量JSON或Provider-native strict mode；没有真实consumer前不扩展这些分支。
- repair仍计入同一AgentLoop总轮次与token使用，但尚未在成本汇总中单列`structured_repair`类别。

## 134. A144：统一Typed Child Result与旧Task Structured Output

### 134.1 InfCodeX参考能力

- `packages/coding/src/workflows/types.ts`的`WorkflowTaskResult`把task ID/name/status、final text、structured、digest、verification、limit、provider/model/route/usage建模为稳定结果，而不是让调用方解析终端字符串。
- `KodaXChildAgentResult`及child executor把child自然结束与structured repair分开：原结论继续是final text，repair只补机器可读字段；一次repair失败不改写原执行终态。
- Agent workflow的前台、并行child和as-tool虽然有不同调度生命周期，但最终consumer读取同一种结果语义。

### 134.2 NZ-Coder原有不足

A143只给声明式AgentGraph接入了structured output。旧`task`仍返回带展示尾注的字符串和一组松散`child_*`字段，background manager又把结果保存成字符串，as-tool使用另一种synthetic message。三条链的身份、usage、verification和structured字段不能由同一consumer稳定读取；恢复旧状态时也没有明确迁移边界。

### 134.3 实现结果

- 新增冻结的`ChildAgentResult`：固定task/name/status/final_text和Session/Agent/parent/trace身份；有界承载structured、verification、changed files、conflicts、provider/model、usage/cost、limit/interrupted及截断事实。
- `to_dict()`是唯一持久wire shape；`to_metadata()`同时发布嵌套`child_result`与旧`child_*`别名。读取时优先canonical envelope，历史Session才经过legacy adapter，避免双写产生两个事实源。
- 前台`task`、background worker完成状态、`agent_manager status`、`apply_agent_changes`和声明式as-tool caller全部投影同一结果对象。`RunEvidence`消费canonical结果，但继续保存原有有界evidence形状，避免证据schema无关扩张。
- 旧`task`与background task schema增加`output_schema`。Schema在首次child运行时验证并固化，resume不得替换；system prompt注入同一输出合同，失败后最多一次继承transcript的`tools=[]` repair。
- repair Assistant、usage和structured evaluation进入child持久Message/Part历史。仅验证成功的对象写入state和结果；repair异常或再次非法不改变child原来的completed/rollback/timeout语义。
- canonical `final_text`只保存模型原始结论；scratch/session/worktree/status等终端展示注记继续留在ToolOutput文本，机器consumer不再收到被展示格式污染的结论。
- `run_subagent`新增参数放在既有`cancel_event`之后，并同步扩展async wrapper，保留旧位置参数调用兼容。

### 134.4 关键设计决策

1. 统一的是结果合同，不强行合并执行引擎。前台task和background manager分别拥有同步resume、线程槽位、取消及apply生命周期；把它们硬塞进一个Runner会破坏现有事务/worktree边界。
2. canonical envelope是事实源，flat字段只做兼容投影。否则新旧consumer各自修改字段后无法判断哪个状态可信。
3. structured显式`null`与“没有structured字段”不同，因此结果保存`structured_present`语义，并只在wire中按实际存在性发布字段。
4. 格式repair只能补结构，不能替换自然语言结论或继续调用工具。这样费用、终态与审计仍能解释一次child真正做了什么。

### 134.5 关键文件

- `nz_coder/runtime/child_result.py`：typed envelope、边界归一化、legacy adapter和state投影。
- `nz_coder/runtime/subagent.py`：前台task structured contract、一次repair及canonical结果producer。
- `nz_coder/runtime/agent_manager.py`：background持久结果、status集合与apply结果producer。
- `nz_coder/runtime/loop.py`：as-tool synthetic caller结果与lineage consumer。
- `nz_coder/run_evidence.py`：canonical child结果到现有证据schema的有界投影。
- `tests/test_child_result.py`及相关subagent/manager/structured测试：wire、兼容、repair、持久恢复和三路径集成合同。

### 134.6 验证结果

- `child_result/subagent/agent_manager/handoffs/structured_output/run_evidence`聚焦101项通过。
- Ruff通过；相关模块Python导入/编译随测试完成。
- 排除已确认由本机损坏matplotlib `.pth`污染原始Bash输出的单项后，完整回归1202项通过，1项deselect，1条multiprocessing fork弃用警告。
- 未运行SWE-bench、付费Provider、真实网络或公网MCP。

### 134.7 学习重点

1. “子Agent有结果”不等于“有结果协议”。终端字符串适合人看，但调度、证据、恢复和SDK必须共享稳定typed envelope。
2. 多仓库思想组装时应先确定NZ里的状态owner，再翻译上游语义；统一API不要求抹掉不同生命周期边界。
3. 兼容迁移应是canonical-write/legacy-read，而不是永久维护两套可写对象。
4. structured repair本身也是一次Provider步骤，必须持久Assistant和usage，否则崩溃恢复后的Session历史无法解释最终对象来源。

### 134.8 剩余差距

- 结果已统一，但前台task与background manager仍有不同启动API；这是生命周期差异，不是结果合同缺失。以后若出现SDK consumer，可再增加一个只负责声明/启动的薄层，而不复制执行逻辑。
- `digest`字段已在wire预留但NZ当前没有独立artifact digest producer；没有真实consumer前不伪造摘要。
- structured repair费用进入child总usage/cost，尚未单列`structured_repair`分类；更完整JSON Schema、流式JSON和Provider-native strict mode仍未实现。
- 跨进程background队列和多进程lineage writer仍未对齐。

## 135. A145：Child RouteFacts与语义模型层级

### 135.1 源码依据与原有差距

InfCodeX `KodaXChildRouteFacts`与`child-executor.ts`记录requested tier、tier outcome、route source、初末Provider/model、fallback、iterations、token和duration。NZ此前结果只有最终Provider/model和总usage，无法解释“请求fast但为什么仍用父模型”，也无法审计write child是否被廉价模型错误降级。

### 135.2 实现与边界

- `model_hint=fast|balanced|deep`在child启动前解析并在resume时冻结；`SUBAGENT_EXPLORE_MODEL`作为fast tier，新增可选`SUBAGENT_DEEP_MODEL`。
- fast只允许read-only child；write-capable child确定性返回`fast-write-ineligible`并继承父模型。未配置tier返回`unconfigured`和明确fallback reason，不伪称已应用。
- canonical result持久requested/outcome/provider/model source、初末route、Assistant迭代数、input/cache/output token和累计duration；background与前台共同消费。

### 135.3 验证与学习

- 覆盖fast实际选模、write防降级、usage/iteration/duration投影及结果round-trip，纳入114项聚焦与1215项全量回归。
- 学习点：路由不是最终model字符串；请求意图、解析结果和fallback原因必须同时存在，才能评估成本与质量策略是否真的执行。

## 136. A146：Workflow EvidenceRefs安全Briefing

### 136.1 源码依据与原有差距

InfCodeX `assertValidWorkflowEvidenceRefs()`在spawn时只接受`file:/diff:/finding:/task_id:`，并拒绝空task ID、未知先前task及把Agent name误作结果引用；`buildChildBriefing()`再经过总token guardrail。NZ只能把证据复制进prompt，自由字符串会静默丢证据或产生路径逃逸风险。

### 136.2 实现与边界

- 新增`child_contracts.py`作为task声明合同owner；最多20个ref，逐项非空、去重、长度有界，未知prefix立即失败。
- `file:`和`diff:`必须解析在parent workspace内；file以UTF-8 replacement读取，diff使用无shell argv、5秒deadline；`task_id:`必须是当前parent精确owned且已有terminal text的child。
- briefing逐项6000字符、总量16000字符，并明确标为untrusted context；output schema仍最后注入，保持机器输出framing。声明进入state/result，resume不得替换。

### 136.3 验证与学习

- 覆盖prefix/空值/逃逸/文件/finding/prior task解析、prompt注入和result投影，纳入114项聚焦与1215项全量回归。
- 学习点：evidence ref是有类型的provenance，不是prompt字符串缩写；必须在spawn边界失败，不能让child在错误上下文中“成功”。

## 137. A147：Machine-checkable Child Postconditions

### 137.1 源码依据与原有差距

InfCodeX `WorkflowTaskVerification`/`evaluateVerification()`支持hard/warn、requiresMutation、requiredChangedPaths、requiredReadPaths、minFinalTextChars和rejectPreparatoryFinalText，结果包含reasons与实际evidence。NZ只有general-purpose固定`verify_changed_files`，无法约束只读review必须读过目标文件，也无法拒绝“我接下来会修改”的假终态。

### 137.2 实现与边界

- task/background新增同名verification对象，声明期拒绝未知字段、不安全路径、错误类型和无界数值，并在resume冻结。
- host从持久ToolPart读取成功read/write事实，从ChangeTracker/worktree读取changed files；模型自然语言不能伪造mutation/read evidence。
- hard失败结算为`verification_failed`，warn失败为`completed_unverified`；typed result保存ok/enforcement/reasons/changed/read/mutation tools，终端文本附加可读原因。

### 137.3 验证与学习

- 覆盖mutation、changed/read path、短文本、preparatory判断、unsafe声明和真实child终态，纳入114项聚焦与1215项全量回归。
- 学习点：verification是side-effect contract，与structured output的return-shape contract正交；两者不能用同一个JSON校验替代。

## 138. A148：一次Same-session Verification Repair

### 138.1 源码依据与原有差距

InfCodeX workflow adapter在hard postcondition失败时生成带原任务、旧final text和具体reasons的repair bundle，累计usage/route facts，并有严格repair上限。A147若直接失败只能报告问题，不能让child修正漏写文件或过早结束。

### 138.2 实现与边界

- hard失败追加synthetic `_nz_verification_repair` User消息，包含原任务、旧结论和机器reasons；继续使用同一child Session、worktree、Message/Part、usage和trace。
- 只有实际进入repair后才额外开放一个Agent迭代；声明verification但未失败不会暗增普通轮次。第二次仍失败确定性结算，不递归repair。
- write child第一阶段事务先结算，再为repair建立新事务；修复后的changed files与固定`verify_changed_files`重新采集，最终postcondition再评估。

### 138.3 验证与学习

- read-only短报告一轮修复成功、连续失败终结、write child从无mutation到写入required path并验证通过均有真实Fake Provider多回合测试；route iterations验证累计为2/3。
- 同时修复structured repair重复持久Assistant的问题：`_new_child_assistant()`已拥有append职责，一个repair请求现在严格对应一个Assistant。
- 学习点：repair预算必须由状态机而非prompt约束；“最大轮次+1”若无条件开放，会悄悄改变所有声明verification的任务行为。

## 139. A149：Full Result与Presentation Summary分离

### 139.1 源码依据与原有差距

InfCodeX `WorkflowTaskResult.finalText`始终供合成/审计，`digest/summaryKind`单独供live panel与历史摘要；digest失败会降级excerpt而不丢full result。NZ background status直接截取包含展示尾注的`background_result`，consumer无法判断这是完整结论还是摘要。

### 139.2 实现与边界

- 所有child终态生成最多800字符的确定性presentation excerpt，并明确`summary_kind="excerpt"`；没有额外LLM调用，因此不冒充model-authored digest。
- canonical `final_text`继续保存完整模型结论；终端session/worktree尾注仍只属于ToolOutput展示。background status优先消费canonical digest，as-tool result也携带同一summary语义。
- 前台、后台、早期错误与取消路径都会持久canonical result和excerpt；legacy state才回退旧字符串。child state自身保存`child_result`，不只依赖父ToolPart。

### 139.3 验证与剩余差距

- 覆盖长文本边界、truthful kind、status consumer、as-tool和child state持久化；五阶段联合114项聚焦、Ruff/编译及1215项完整回归通过。
- 未实现InfCodeX可选异步LLM digest和late summary event；当前没有必要为UI摘要增加第二次付费请求。若以后实现，必须保留`pending/result/digest-failed`状态和run停止后的late-update丢弃规则。
- 未运行SWE-bench、付费Provider或公网服务。

## 140. A150：Revisioned Workflow Process Snapshot

### 140.1 InfCodeX源码依据

`packages/agent/src/workflow/process.ts`将run状态与task item状态分开，统一生成`WorkflowProcessSnapshot`：run identity/status/time、items、counts、spawned/finished/active/failed/stopped progress、token、latest message和result summary。REPL/SDK订阅该snapshot，而不是分别扫描child对象拼装状态。

### 140.2 NZ原有不足与实现

Agent Manager的`status()`直接读取child state文件并拼字符串；HTTP/CLI或未来SDK若要计算进度、失败数和token只能复制逻辑。进程重启时又只能看到多个离散state，无法判断某次状态投影的revision。

- 新增`WorkflowProcessStore`，为每个parent Session拥有一个`background-agents` process；item稳定以child session ID标识。
- snapshot固定`schema_version/revision/run_id/status/time/items/counts/progress/tokens/latest_message`，并把child raw status归一为pending/running/completed/failed/cancelled。
- canonical child digest映射为summary，`summary_kind`映射到pending/result/notice/unavailable；usage、provider/model、changed files一并有界投影。
- `agent_manager status`在返回人类文本的同时发布`workflow_snapshot` metadata，机器consumer不再解析Rich/文本输出。
- child state仍是执行事实源；snapshot是事件归约后的只读投影，manager启动和status边界通过幂等reconcile补齐外部状态变化。

### 140.3 验证与边界

- 覆盖revision递增、状态/count/progress/token/summary映射、双background并发完成和reconcile幂等；纳入128项组合与1220项全量回归。
- 当前一个parent Session只有一个background process；尚未实现多个命名workflow run并存、phase item、artifact item和resume cache origin。

## 141. A151：Append-only Workflow Events与Replay

### 141.1 InfCodeX源码依据

`workflow/runtime.ts`以`WorkflowEventRecorder`记录run graph事实，`process.ts::applyEvent()`把agent spawned/completed/stopped/summary updated等事件归约为snapshot，并向host发布`workflow_started/updated/finished`。事件是恢复和订阅边界，snapshot只是物化缓存。

### 141.2 实现结果

- queued、started、cancel-requested、terminal和startup reconcile全部写入独立JSONL journal；每条含run ID、sequence、parent event ID、timestamp、task ID和有界item。
- append使用0600、`O_APPEND`、fsync；snapshot用0600临时文件+fsync+atomic replace。每条最多64 KiB、journal最多16 MiB/100000条。
- 加载时验证run、sequence和parent chain。仅最后一条没有换行时按崩溃截断忽略；任何完整坏JSON或链断裂立即拒绝，避免把有缺口的事件历史伪装成可重放。
- snapshot文件缺失时完全从event log重建并重新物化；不信任旧snapshot覆盖journal事实。
- `agent_manager action=events after_sequence=N`返回严格大于cursor的不可变事件后缀及当前snapshot/revision，形成SDK/SSE未来可直接消费的本地协议。
- 进程重启无法安全接管原线程时，既有live child仍按原策略转为`interrupted`，随后以`task_reconciled`显式进入日志；不会把丢失执行器伪装为继续运行。

### 141.3 验证与学习

- 覆盖snapshot删除后的纯事件重放、截断尾恢复、完整损坏拒绝、幂等reconcile和events cursor；128项聚焦、Ruff/编译及1220项完整回归通过。
- 学习点：恢复协议必须区分“物化缓存丢失”和“事实日志损坏”。前者可重建，后者必须失败；简单扫描state并覆盖snapshot会掩盖生命周期断点。
- 剩余差距：尚未把workflow event直接桥接进公共`SessionEventBus`/HTTP SSE，也没有跨进程单writer锁；因此当前准确边界是同一parent Session的本地单进程持久恢复，而不是分布式workflow service。

## 142. A152：Workflow Wait、Stop、Timeout与Cleanup唯一终态

### 142.1 InfCodeX源码依据

`workflow/runtime.ts::doWait()`为spawn+wait和runAgent提供同一abort/terminal gate；wait失败或abort会有界调用backend stop，`terminalTaskIds`保证每个task只发一个terminal event。`agent-adapter.ts::wait()`把timeout作为包括verification repair在内的总deadline，而不是每个attempt重新计时。workflow结束还会停止未等待child并释放并发容量。

### 142.2 NZ原有问题

- `status(wait_ms)`只允许等一个任务最多10秒，不是workflow wait；批量consumer只能轮询并自行处理顺序、deadline和超时。
- `cancel()`只设置event后立即返回，`close()`直接设置event；stop reason、cancel-requested event和terminal settlement并非同一状态机。
- worker异常或重复stop可能让同一task写多个terminal event；`AgentLoop.close()`此前没有结算background manager，未等待child可能在Session事件和trace关闭后继续运行。

### 142.3 实现结果

- 新增`agent_manager action=wait`：验证所有task属于当前parent，按调用方ID顺序返回typed child results；多个task共享一个最多600秒的deadline，不会把N个timeout串成N倍等待。
- wait超时收集当前及剩余live task，统一进入stop request并最多再等待2秒settle；metadata明确区分`timed_out_task_ids`和仍未结算的`unsettled_task_ids`。
- 新增`action=stop`：原因有界持久化，cancel-requested event只写一次，多个调用幂等；支持最多30秒共享settle deadline。旧`cancel`保留为同一transition的兼容别名。
- `WorkflowProcessStore`在replay时重建explicit terminal ID集合，重复`task_terminal`不追加revision；state reconcile仍可记录后续事实修正，但不冒充第二次终结。
- `BackgroundAgentManager.close()`使用同一stop owner结算所有未等待process-local child；deadline内未settle会明确抛错且manager实例保持可达。
- `AgentLoop.close()`先执行background cleanup，再关闭MCP、SessionEventBus和trace。即使cleanup失败也完成其余资源关闭，最后把原错误返回调用者。

### 142.4 验证结果

- 新增/增强5类专项：批量result顺序、wait timeout→stop→settled、重复stop仅一个cancel/terminal event、close结算未等待child、AgentLoop资源关闭顺序。
- Agent Manager/Workflow专项24项通过；Ruff、编译检查通过。
- 排除既有损坏matplotlib `.pth`污染原始Bash输出的单项后，完整回归1225项通过，1项deselect，1条multiprocessing fork弃用警告。
- 未运行SWE-bench、付费Provider或公网服务。

### 142.5 学习重点与剩余边界

1. wait timeout必须覆盖完整task生命周期及repair，不应在每次内部尝试重新获得完整timeout。
2. stop不是“set一个Event”；它包含幂等请求事实、合作取消、settle deadline、唯一terminal和容量释放。
3. Session资源销毁顺序必须让child先停止，否则late worker会向已关闭事件/trace owner写入。
4. 当前Python线程无法被安全强杀。超过cleanup deadline时NZ选择抛错并保留manager，而不是谎报已停止；真正的强隔离需要子进程执行器。

## 143. A153：Workflow Fan-out并发池与生命周期额度

### 143.1 InfCodeX参考能力

- `packages/agent/src/workflow/runtime.ts`中的`runPool()`以固定lane消费lazy thunks，按输入index写结果；普通child失败归一为`null`并继续兄弟任务，abort、agent cap和budget等run-control错误才终止调度。
- 同文件的runtime把`maxAgents`定义为整个run累计spawn额度，把`maxConcurrency`定义为活跃Agent semaphore额度；spawn在等待容量前后都检查结构性限制，任务结算后只释放一次容量。
- `packages/llm/src/run-scoped-config.ts`为并发设置默认值与绝对上限，避免缺省配置变成无界fan-out；`child-executor.test.ts`直接测量实际峰值并验证单child失败不影响兄弟。

### 143.2 NZ-Coder原有不足

- 已有`BoundedSemaphore`能限制某一时刻执行的线程，但`start()`只验证本次列表不超过`SUBAGENT_BACKGROUND_MAX_TASKS`。同一parent连续或并发调用多次`start()`可以绕过workflow生命周期总额度。
- admission校验、state发布与thread注册不在同一个原子区间；两个调用可能同时看到相同剩余额度或路径状态。
- worker先释放semaphore、后写terminal event。下一任务可以先进入running，使事件快照短暂报告超过真实并发上限的峰值。
- start只返回文本，consumer无法直接获得按输入顺序排列的task IDs、fan-out identity和本次采用的两个限制。

### 143.3 实现结果

- Manager构造时冻结`agent_cap`与`concurrency_cap`；并发额度被限制在`[1, agent_cap]`，不会比生命周期额度更宽。
- 每个start先统计当前parent已发布的全部background task；累计`spawned + requested`超过`maxAgents`时整批拒绝，已完成/失败任务也不会返还生命周期额度。
- batch schema、路径、兄弟重叠、活跃child冲突、state/event发布和job注册进入同一个`RLock` admission边界。失败发生在发布前时整批不产生child state。
- 每批生成稳定`fanout_id`，每个child持久`fanout_index`；start ToolOutput返回有序`task_ids`、两个cap和当前snapshot。既有文本接口保持兼容。
- 普通child异常只把自身结算为`error`，其他lane继续；`wait`继续按请求ID顺序返回canonical results。
- terminal state/event现在先于slot release完成；WorkflowProcessStore从可重放事件计算`peak_active_agents`，并在snapshot公开`agent_cap`与`concurrency_cap`。

### 143.4 关键设计决策

- 没有另写第二套parallel executor：现有后台thread是实际child生命周期owner，给它补齐InfCodeX的额度和pool语义比增加一个只包装Future的空层更一致。
- 生命周期额度按已持久化task计数，而不是按`_jobs`计数；因此manager重建后仍不能通过重启绕过cap。
- 容量只在terminal发布后归还。这样会让slot多占用极短的fsync时间，但保证事件consumer看到的running数量永远不超过真实调度容量。

### 143.5 关键文件

- `nz_coder/runtime/agent_manager.py`：原子fan-out admission、双限制、ordered metadata、terminal-before-release。
- `nz_coder/runtime/workflow_process.py`：concurrency cap与可重放peak active投影。
- `tests/test_agent_manager.py`：真实线程峰值、失败隔离、稳定顺序、跨调用生命周期cap和并发admission竞态。
- `tests/test_workflow_process.py`：snapshot双cap与峰值合同。

### 143.6 验证结果

- 静态检查：相关4个Python文件Ruff通过。
- 定向测试：Agent Manager与Workflow Process共26项通过，包含3任务/2 lane的真实线程峰值、其中一项抛异常后兄弟完成、第二次start消耗剩余累计额度，以及两个并发start争夺最后一个额度只能成功一个。
- 完整测试：排除已确认由本机损坏matplotlib `.pth`污染原始Bash输出的单项后，1227项通过，1项deselect，1条Python 3.13 multiprocessing fork弃用警告。
- 真实冒烟：本轮使用真实thread/semaphore/state/event/fsync路径，Provider由fixture替代，未发送付费请求。
- 是否运行评测：未运行SWE-bench。

### 143.7 学习重点

1. `maxAgents`和`maxConcurrency`解决不同问题：前者限制run总成本/图规模，后者限制瞬时资源；把两者合成一个“每批最多N项”会留下跨批绕过。
2. 并发正确性不只看worker计数。admission必须把“检查剩余容量—声明路径—发布任务”做成一个原子决定。
3. 事件顺序属于调度语义。先release再terminal即使只差几毫秒，也会让可重放快照产生实际不可能出现的峰值。
4. 失败隔离只吞普通task失败；结构性额度、全局abort和预算错误不能伪装成一个普通失败项。NZ当前start在派生前整批拒绝结构性cap，运行期异常则只结算对应child。

### 143.8 剩余差距

- 当前提供后台start/wait原语，尚未实现InfCodeX `wf.parallel()`、`wf.pipeline()`和phase-aware map/reduce声明式脚本层；下一阶段应先增加final synthesis gate，再决定是否需要完整workflow DSL。
- Python thread无法强杀不合作的Provider；进程级child executor仍是后续可靠性边界。
- workflow event尚未桥接公共SessionEventBus/HTTP SSE，外部consumer目前通过`action=events`拉取。

## 144. A154：Gated Final Synthesis

### 144.1 InfCodeX参考能力与NZ原有不足

InfCodeX `packages/agent/src/workflow/runtime.ts::synthesize()`不调用backend旁路，而是用`runAgentImpl({name:'synthesize', readOnly:true})`启动真实Agent；因此合成同样受maxAgents、maxConcurrency、budget、abort和event约束。NZ此前只能由父模型自行阅读background status文本，没有“最终结论必须经过同一runtime gate”的机器边界，也可能只拿excerpt而丢失full result。

### 144.2 实现、设计与关键文件

- `WorkflowRuntime.synthesize()`只接收完整canonical child `final_text`与非空rubric，构造有界、明确禁止虚构证据的最终fold prompt。
- synthesis通过`BackgroundAgentManager.start()`启动`read_only=True`、`model_hint=deep`的Child，再由同一`wait()`结算；它真实消耗生命周期/并发额度并产生queued/started/terminal事件。
- 所有workflow Child显式屏蔽`workflow_run`和`agent_manager`，不能在没有nested-workflow合同的情况下递归扩大Agent图。
- 合成失败不是普通map item，直接使workflow失败；成功后追加`synthesis_completed`事实。最终ToolOutput发布该Child的完整final text和整个workflow result。
- 关键文件：`nz_coder/runtime/workflow_runtime.py`、`nz_coder/runtime/agent_manager.py`、`tests/test_workflow_runtime.py`。

### 144.3 验证、学习与剩余差距

专项测试证明2个调查Child+1个synthesis恰好消耗3个agent，且synthesis prompt包含两个完整结果和rubric。学习点是“合成”也是Agent effect，不能用不计费、不发事件的Provider隐藏调用绕过runtime。当前还没有独立sidecar verifier对合成结论二次审核，这是A159候选。

## 145. A155：Phase、Pipeline与Map-Reduce Workflow原语

### 145.1 InfCodeX参考能力与NZ原有不足

InfCodeX runtime的`phase()`发started/finished事件；`parallel()`用固定lane、按index写结果；`pipeline()`让每个item完成自身stage N后立即进入N+1，没有全局stage barrier；普通stage失败只丢弃该item。NZ A153只有后台start/wait原语，父Agent仍需手工组织依赖和合成，不能声明一个可审计执行图。

### 145.2 实现、设计与关键文件

- 新增注册工具`workflow_run`，只接受受限数据计划，不执行任意Python/JS脚本。phase支持`parallel`、`pipeline`、`map_reduce`和`synthesize`四种mode。
- `parallel()`使用有界ThreadPoolExecutor并保留输入顺序；普通Child异常归一为`None`，结构性`WorkflowControlError`继续抛出。
- `pipeline()`为每个item建立顺序stage chain，各chain并行推进；模板只替换`{item}/{previous}/{index}`，物化prompt有60K上限。
- `map_reduce`先failure-isolated map，再把非空结果交给A154 gated synthesis；全部spawn仍走同一Manager semaphore和lifetime cap。
- 关键文件：`nz_coder/runtime/workflow_runtime.py`、`nz_coder/runtime/prompt.py`、`tests/test_workflow_runtime.py`。

### 145.3 验证、学习与剩余差距

真实thread测试让slow item的stage 1延迟，证明fast item已进入stage 2而无需等slow barrier；结果仍按输入顺序返回。另测3项map中1项异常，另外2项完成且reduce仍运行。学习点是pipeline并发单位是“item chain”，不是“全体stage barrier”。当前计划DSL没有循环、嵌套workflow和动态spawn；这些应在实际consumer需要时增加，不能开放任意代码执行代替受限协议。

## 146. A156：Workflow Quality Preflight

### 146.1 InfCodeX参考能力与NZ原有不足

InfCodeX `quality-lint.ts`在restricted script执行前检查未await command、structured字段误读和literal fanout超过maxAgents；`script-runner.ts`只有lint通过才运行。NZ若边执行边发现计划错误，会留下部分Child、额度和事件，无法称为preflight。

### 146.2 实现、设计与关键文件

- `lint_workflow_plan()`在构造Runtime前验证plan/phase/task类型、phase唯一名称、合法mode、items/stages/tasks、read_only类型、write target scope、rubric、只引用先前phase、正整数concurrency和256项fanout边界。
- 静态计算parallel task数、pipeline items×stages、map items+reduce及synthesis额度，与Manager剩余生命周期cap比较；多结果计划默认强制最后以map_reduce/synthesize收口。
- 所有finding具有稳定code/severity/message；`workflow_run`以ToolOutput metadata返回完整findings，任何error都在child state/event发布前整批拒绝。
- 关键文件：`nz_coder/runtime/workflow_runtime.py::lint_workflow_plan`、`tests/test_workflow_runtime.py`。

### 146.3 验证、学习与剩余差距

组合错误计划一次命中invalid concurrency、write-without-scope、literal fanout cap和missing synthesis，同时断言spawned count仍为0。学习点是quality lint必须处于effect admission之前；运行到一半再报错只是failure handling。当前是声明式plan lint，不解析Python/JS source，因此不存在InfCodeX的“忘记await Promise”语法问题；若未来引入脚本宿主，必须重新建立AST级restricted runner，而不能复用字符串正则冒充。

## 147. A157：Content-addressed Same-session Resume Cache

### 147.1 InfCodeX参考能力与NZ原有不足

InfCodeX用canonical sorted-key spawn input的SHA-256加同输入occurrence构成cache key；只缓存成功runAgent结果，resume从prior run读取并copy-forward到current run，corrupt entry视为miss；最终synthesis刻意每次fresh。NZ已有Session/Child恢复，却会重新执行声明式workflow中没有变化的成功调查任务。

### 147.2 实现、设计与关键文件

- `WorkflowResultCache`把结果保存到workflow私有`runs/<run_id>/results/`，run ID只接受安全字符；entry用0600原子replace，目录尽力设0700。
- key为canonical task JSON的SHA-256前24位加occurrence；occurrence map有锁，parallel相同输入不会竞态复用同一key。
- 只接受可反序列化为`ChildAgentResult`且状态为completed/completed_unverified的entry；失败、损坏、超大或symlink均为miss。
- prior命中会copy-forward到current run，使下一次resume不再依赖祖先目录；命中发`task_replayed`而不产生虚假spawn。A154 synthesis明确绕过cache并fresh执行。
- 关键文件：`nz_coder/runtime/workflow_runtime.py::WorkflowResultCache`、`tests/test_workflow_runtime.py`。

### 147.3 验证、学习与剩余差距

两次同plan运行证明第二次调查Child只执行一次、replayed_agents=1，而synthesis执行两次；另验证0600、corrupt miss和failed result不落盘。学习点是cache必须缓存“成功effect结果”而非展示文本，也必须用occurrence区分同一输入在同run中的多次合法调用。当前没有跨workspace/global cache，也不会缓存写Child的外部副作用重放；这是刻意的安全边界。

## 148. A158：Workflow Event桥接SessionEvent与HTTP SSE

### 148.1 InfCodeX参考能力与NZ原有不足

InfCodeX workflow recorder把run graph event归约成process snapshot，再由host发布workflow started/updated/finished供REPL/SDK消费。NZ A151已有可靠journal和events cursor，但公共SessionEventBus/HTTP SSE看不到workflow phase、replay或synthesis，只能由调用者轮询专用工具。

### 148.2 实现、设计与关键文件

- WorkflowProcessStore增加phase_started/finished、task_replayed和synthesis_completed合法事件及`record_event()`；这些仍进入同一sequence/parent chain、fsync和snapshot revision。
- `_append()`先完成journal和atomic snapshot，再调用best-effort presentation sink；live publish失败不能回滚或伪造持久事实。
- BackgroundAgentManager绑定所属AgentLoop的现有SessionEventBus，把每条journal event映射为`workflow.<event.type>`，properties同时携带immutable event和对应revision snapshot。
- AgentLoop构造/恢复均重新绑定bus；HTTP服务本来就把同一bus编码为SSE，因此不增加第二套route、cursor或broker。
- 关键文件：`nz_coder/runtime/workflow_process.py`、`nz_coder/runtime/agent_manager.py`、`nz_coder/runtime/loop.py`、`nz_coder/session_events.py`（复用，未建立平行协议）。

### 148.3 验证、学习与剩余差距

专项测试真实运行synthesis plan并观察phase.started→task queued/started/terminal→synthesis.completed→phase.finished，terminal snapshot包含phase；最后一条可由现有`encode_sse()`直接生成带event ID的帧。学习点是journal是事实源、SessionEvent是实时投影，两者失败语义不能倒置。当前没有专门的Workflow TUI reducer，CLI只会通过通用事件能力收到数据；产品展示后续应消费这些事件，而不是另扫state文件。

## 149. A159：独立Sidecar Verifier

InfCodeX的sidecar verifier使用与合成Agent隔离的新上下文审核结果，并返回`accept/revise/blocked`。NZ新增fresh read-only/deep验证Child：`accept`发布，`revise`把具体理由注入新一次fresh synthesis，`blocked`终止workflow；返工上限为0–2。Provider或结构化协议失败采用上游的fail-open，但保留`sidecar_failure`事实，不能伪称验证成功。验证Child与返工Child均计入Agent、token和事件额度。

关键文件为`runtime/workflow_runtime.py`、`runtime/workflow_process.py`和`tests/test_workflow_runtime.py`。专项覆盖accept、revise后accept、blocked/失败语义的基础路径。剩余差距是尚未用公网Provider验证不同模型遵守结构化verdict的稳定性；本轮未运行付费Provider或SWE-bench。

## 150. A160：Workflow Token Budget与Abort

对齐InfCodeX runtime在spawn前检查`tokenBudget`、Child终态累计usage、abort停止active tasks的控制语义。NZ的`WorkflowRuntime`现在区分普通Child失败与`WorkflowBudgetError/WorkflowAbortError`结构错误；每个Child结算后累计output/total tokens，下一次spawn前硬门禁并发布`budget_updated`。caller cancellation会停止当前所有active Child，只发布`workflow_run_stopped`；其他结构错误发布`workflow_run_failed`。

`BackgroundAgentManager.wait_until_settled()`只承担无副作用等待，不虚构timeout。专项证明7-token Child耗尽预算后不会启动synthesis，以及运行中abort只产生一个task terminal。剩余差距是Provider usage缺失时无法凭空精确计费，只能依赖已有typed usage producer。

## 151. A161：可选Spawn-process Child隔离

InfCodeX主要依赖AbortController合作取消；NZ的Python线程同样无法强杀不合作的SDK调用，因此在保持默认thread路径的同时增加`isolation: process`。该路径使用`multiprocessing spawn`建立真正进程边界，Child继续写同一workspace-owned持久state；停止时依次terminate、有界grace、必要时kill，父Manager仍是唯一terminal发布者。

配置为`NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED`与`NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS`，计划preflight和Agent Manager schema都会拒绝未知模式。真实测试用完全不响应cancel的30秒Child验证在2秒stop窗口内结算为cancelled。限制是进程Child不共享内存mailbox/event bus对象，只通过持久state和父进程终态桥接；需要即时peer messaging的任务应继续用thread模式。

## 152. A162：Workflow Outcome进入Session Lineage

NZ已有三级记忆与append-only Lineage，但workflow完成结果此前只存在ToolOutput和workflow journal。现在AgentLoop把所属`SessionLineage`注入Manager，成功workflow按`workflow:<run_id>`幂等追加`memory_outcome_digest`，并记录`memory_outcome_recorded`事件。

摘要只保留status、phase names、最多20个task IDs、replay/budget、canonical digest及sidecar verdict，不保存raw `final_text`，也不自动写入可长期召回的用户MemoryManager；这避免工具在未获长期记忆写权限时扩大数据留存。专项验证重复记录无第二条Lineage事实且payload不含raw输出。失败workflow仍以workflow journal为事实源，尚未写入记忆摘要，避免把未完成结果当经验。

## 153. A163：Machine-readable Workflow Parity Contract

新增`runtime/workflow_contracts.py`，初始版本`1.0`固定四种phase、三类唯一run终态、sidecar verdict、普通失败隔离/结构失败停止、成功缓存与fresh synthesis、额度包含synthesis、token spawn前检查、thread/process隔离和有界Lineage结果语义；A164–A168加入managed run与artifact/run record后升级为`1.1`。每次workflow结果携带该contract的防御性副本，SDK、trace和回归测试不再靠阅读实现猜测行为。

`tests/test_workflow_parity_contracts.py`逐项锁定上述源码级决策，并验证非法isolation在effect admission之前拒绝。本轮Ruff、85项核心组合及完整回归1251 passed、1 deselected通过；唯一warning来自既有SWE测试的Python 3.13 fork deprecation，启动时matplotlib `.pth`噪声仍是环境问题。未运行SWE-bench或付费Provider。

## 154. A164：严格Workflow Manifest与Admission交叉校验

依据InfCodeX `packages/agent/src/workflow/manifest.ts`新增`runtime/workflow_manifest.py`。Manifest严格声明name、description、phase顺序、read-only、planned/max agents、max concurrency、token budget、worktree能力和六种合法pattern；未知字段语义不靠宽松强转掩盖。Preflight将声明与真实plan交叉检查，包括read-only违约、phase不一致、Session剩余额度、并发上限、token不一致，并把sidecar最坏返工路径计入planned spawn，所有错误仍发生在首个Child发布前。

学习点是manifest不是展示标签，而是effect admission合同；只解析JSON却不核对执行图没有安全价值。当前未引入InfCodeX受限JavaScript runner，NZ继续使用不可执行的声明式plan，避免开放任意脚本宿主。

## 155. A165：Managed Workflow Run生命周期

依据InfCodeX `run-manager.ts`在现有BackgroundAgentManager内加入run registry及running/paused/completed/failed/stopped状态。Pause只门禁下一次Agent spawn，不假装冻结已运行Child；resume唤醒等待者；stop唤醒paused线程并按`workflow_run_id`取消活跃Child。`agent_manager`新增run_list/run_pause/run_resume/run_stop操作，终态run最多保留500条内存快照。

真实并发测试覆盖paused时零spawn、resume后继续、paused stop以及active Child stop。审查中修复了“最后一个Child被stop后workflow仍发布completed”的竞态：Child结算后和最终发布前都重新检查run stop，最终只能进入stopped。当前registry和InfCodeX一样是进程内控制面；持久历史由A168 run.json承担，重启后不会把旧run恢复成可pause对象。

## 156. A166：有界Workflow Artifact

依据InfCodeX `WorkflowApi.artifact()`与`run-graph.ts`新增`WorkflowRunStore`。Phase可用`artifact`声明保存其完整结构化结果；名称清理为最多120字符的安全文件名，内容必须可JSON化且不超过2MiB，目录0700、文件0600、临时文件fsync后原子replace。成功后才发布`artifact_written`及相对引用，workflow outcome和run.json共享同一引用。

Artifact只允许数据，不允许脚本、绝对路径或workspace外写入。当前相同安全化名称会覆盖前一artifact，preflight已拒绝完全相同的声明名；若未来开放动态artifact名称，应再加入collision-resistant identity。

## 157. A167：Durable Workflow Log

依据InfCodeX `WorkflowApi.log()`增加1–4000字符结构化progress log。声明式phase的`log`在phase结果和artifact成功后写入同一append-only workflow journal，事件类型为`workflow_log`，自动沿A158桥接到SessionEvent和HTTP SSE。Payload仍受WorkflowProcessStore 32KiB边界约束，live publish失败不影响已fsync事实。

这里没有新建独立日志文件或logger，因为workflow journal已是顺序、replay和cursor的唯一事实源。当前plan只支持phase完成日志；运行中Child细粒度状态继续由既有task/message events表达。

## 158. A168：Terminal Run Record与Efficiency Report

依据InfCodeX `run-graph.ts`和`cost-report.ts`，每个run现在原子写入私有`run.json`，包含run identity、started/ended、terminal status、phase、artifact引用和efficiency report。报告从属于该`workflow_run_id`的canonical Child state聚合input/output/cache/total token、Agent starts、child turns、各终态、token coverage和wall-clock；usage缺失会列出task ID，不用0伪装完整覆盖。

Workflow outcome同步返回同一report，机器contract在本阶段升级到1.1并声明managed-run、artifact与run-record语义，A169–A173加入Capsule后继续升级为1.2。专项验证artifact权限、安全名称、7-token准确聚合、coverage、log/artifact事件和run.json一致性；142项核心组合、完整回归1258 passed/1 deselected及Ruff通过。唯一warning仍是既有Python 3.13 SWE fork deprecation；未运行SWE-bench和付费Provider。

## 159. A169：JSON-only Workflow Capsule Contract

参考InfCodeX `packages/agent/src/workflow/capsule.ts`新增`runtime/workflow_capsule.py`，固化format/version/workflow API/min NZ version、manifest、plan、intent、requirements和provenance。NZ没有复制上游Capsule中的受限JavaScript `source`：Capsule只允许声明式JSON plan，显式拒绝`source`和未知顶层字段，因此保存/查看Capsule不会变成本地代码执行入口。Manifest被规范化后注入plan，并拒绝Capsule与plan声明不一致。

该实现是能力语义对齐，不宣称与`kodax.workflow`文件二进制兼容；两者安全模型不同。若未来需要导入上游Capsule，必须先实现独立转换器和source→JSON plan审计，不能直接执行其脚本。

## 160. A170：Capsule Requirements Preflight

对齐InfCodeX `preflightWorkflowCapsule()`：执行前检查minimum semver、Git repository、worktree capability、工具、MCP server、Skill与fast/balanced/deep model tier。明确提供inventory且缺项为error；host没有提供某类inventory时为warning而非伪造成功。自动环境探测只读取现有Tool registry、SkillLoader、`.git`、git executable和worktree配置，不启动MCP、不导入插件、不产生外部effect。

`workflow_run(capsule_name=...)`必须Capsule preflight通过后才进入A164 plan lint；错误时没有Child、run或artifact发布。当前MCP运行时inventory尚未从Session-owned MCP对象注入该只读预检，所以声明MCP要求时会得到可见warning；真正缺失的显式inventory已有error语义。

## 161. A171：Saved Capsule保存与发现

参考InfCodeX `discovery.ts`新增`runtime/workflow_library.py`。Project目录为`.nz-coder/workflows`，personal目录为`~/.config/nz-coder/workflows`；只发现不超过1MiB的普通`*.workflow.json`，忽略symlink，project同名优先。保存先完整验证Capsule，再以0600临时文件、fsync、atomic replace发布；名称只允许安全文件字符且最多80字符。

读操作由`workflow_library`工具提供list/show/preflight并标记read effect；保存由独立`workflow_save`工具提供并标记write effect，避免把查询工具混成隐式写入。与InfCodeX不同，NZ不发现或import `.ts/.js/.mjs`，这是刻意的无可执行Capsule边界。

## 162. A172：Saved Capsule真实执行与Provenance

`workflow_run`现在接受互斥的`plan`或`capsule_name`，可选project/personal source。执行链严格为discover→load/size/symlink/schema→requirements preflight→plan quality lint→原WorkflowRuntime；Capsule不建立新的runtime、Manager、budget或事件系统。Project覆盖personal的结果可用显式source约束，未知或冲突均确定性失败。

来源引用与完整preflight进入workflow outcome；name/source/execution有界投影进入Session Lineage digest，完整Capsule ref同时进入terminal `run.json`。专项从磁盘保存Capsule后通过真实Manager和fake Provider完成synthesis，证明不是只实现了list/load模块。

## 163. A173：Run/Artifact读取与可恢复Retention

参考InfCodeX lifecycle controller新增`workflow_runs`只读工具，安全列出terminal run摘要、读取精确`run.json`及manifest声明的JSON artifact。Run ID、目录归属、symlink、文件大小、artifact相对路径和run identity逐层验证，不能用路径参数读取任意文件。

`workflow_run_archive`是write effect且要求`confirm=true`；running/paused run硬拒绝。所有显式run ID先整体解析，任何未知/损坏目标都会在移动第一个目录前终止，避免部分归档；成功时只移动到runs内私有`.trash`并返回恢复路径，不做不可恢复删除。支持keep/older_than_days候选生成，但同样经过全目标预检。机器contract升级到1.2。6项新增Capsule/Lifecycle、149项核心组合、完整回归1264 passed/1 deselected及Ruff通过；未运行SWE-bench或付费Provider。

## 164. A174：Builtin/Saved Capsule统一解析

参考InfCodeX builtin workflow与discovery链，新增`workflow_resolver.py`作为唯一解析入口。解析顺序固定为trusted builtin→project→personal，因而同名saved Capsule不能遮蔽经过代码审查的内置流程；saved plan只允许对字符串叶节点执行有界`{args.key}`替换，解析后仍走Capsule环境preflight和原plan admission。根`workflow_run(capsule_name=...)`也改用同一入口，避免“内置只能展示、保存的才能执行”的半闭环。

## 165. A175：一级嵌套Workflow

新增`mode: workflow`，在任何Agent effect前递归解析内层Capsule并保存其ref/preflight。`WorkflowRuntime`以同一实例执行内层phase，事件名投影为`outer/inner`，便于journal、SSE与run审计保持因果路径。嵌套深度硬限制为一层；二级嵌套在首个spawn前拒绝，避免递归图让静态Agent上限和安全审计失真。

## 166. A176：嵌套资源与终态共享

嵌套没有创建第二个Runner或Manager：occurrence/cache、Agent生命周期额度、并发池、token budget、abort/stop、artifact、run store和唯一terminal owner全部沿用parent。专项用单个7-token Child耗尽预算，证明inner synthesis不会越过parent门禁；正常路径两个Child总计14 tokens并进入同一run。机器contract据此声明`nested_workflow_depth=1`与`nested_runtime_resources=shared-with-parent`。

## 167. A177：Trusted Builtin Registry

新增`workflow_builtins.py`和`workflow_builtin`只读工具。内置条目是Python代码中构造的严格数据Capsule，不从workspace加载脚本，也不开放动态执行；当前注册`parallel-investigation`和`scoped-review`。这一区分解决“可信产品能力”和“用户保存流程”之间的信任边界，同时复用完全相同的Capsule validator、manifest admission和runtime。

## 168. A178：Parallel Investigation

参考InfCodeX `parallel-investigation.ts`，将问题按目标分配给最多20个只读balanced investigator，每个Child必须产出结构化`finding`，最后由fresh synthesis汇总完整结果。实际计划仍受Session剩余额度和manifest更小上限约束；默认不会为了填满上限复制无意义任务。当前没有照搬上游prompt文本或TS runner，只复现bounded fan-out、结构证据和统一合成语义。

## 169. A179：Immutable Review Packet

参考InfCodeX review packet实现`workflow_review.py`。调用者提供的diff字节只捕获一次，不在审查阶段重新读取Git；按packages/clients/top-level area和source/test/docs/config分类，UTF-8按字节预算切块，写入私有`.nz-coder/review-packets`，文件0600且原子发布。每个packet记录range/content hash、scope paths、requirements、test evidence和routing risk。测试还修正了`tests/*.py`应优先归test而非source的分类边界。

## 170. A180：Scoped Review

参考InfCodeX `scoped-review.ts`，builtin将每个immutable packet送入primary reviewer，再以deep verifier复核，之后执行确定性quality gate并把audit保存为bounded JSON artifact，最后通过正常synthesis Child交付。所有审查Child、verifier和synthesis都计入同一Agent/token额度；端到端专项证明一个packet对应2个审查Child加1个synthesis，而不是在runtime外偷跑Provider。

## 171. A181：Review Quality Gate

新增`quality_gate`零Agent phase。它递归读取validated structured outputs，只删除明确`refuted` finding，保留`confirmed`和`unresolved`，聚合`unverified_requirements`；如果reviewer只说“looks fine”却没有结构化证据，gate显式加入`review output unavailable`并禁止unqualified approval。这样“审查模型失败/沉默”不会被误解释为代码通过。

## 172. A182：Terminal Worktree Sweep

新增`workflow_sweep.py`，workflow终态写record前清扫本run Child worktree，Manager启动时也尝试清理超过6小时的terminal残留。只删除能证明clean的目标：reported changed files、Git实际变化或无法验证clean的write copy都会保留并告警；清理异常是fail-soft，不篡改Agent结果。当前尚未做跨机器租约或后台定时daemon，属于单机Session生命周期闭环。

## 173. A183：六模式JSON-only Workflow Generator

新增`workflow_generate`只读工具，为manifest声明的六种pattern生成严格Capsule：classify-and-act、fan-out-and-synthesize、adversarial-verification、generate-and-filter、tournament、loop-until-done。生成物只有有界JSON数据，不含`source`，还必须再次通过Capsule validator；这保留InfCodeX模板化编排价值而不把LLM生成脚本变成本地代码执行入口。

本轮新增17项专项、81项Workflow/Manager核心组合及1282项完整回归全部通过，另deselect 1项既有live Bash测试；Ruff与`git diff --check`通过。唯一pytest warning是既有Python 3.13 multiprocessing fork deprecation，Conda matplotlib `.pth`启动traceback仍属于环境噪声。机器contract升级为1.3。本轮未运行SWE-bench、付费Provider或公网MCP。仍然存在的外部证据缺口包括公网模型遵守structured schema的稳定性、跨进程/跨机器workflow控制、上游JavaScript Capsule导入兼容和300例SWE-bench官方分数，不能由本地fixture推断已对齐。

## 174. A184：安全Workflow Historical Identity

重新阅读InfCodeX `workflows/identity.ts`后新增`runtime/workflow_host.py`。解析只读取私有runs目录内通过既有run-record校验的terminal记录；run ID必须满足安全字符与目录归属，missing或损坏记录不会被猜测成可恢复run。该模块复用A173 reader，不建立第二套历史存储。

## 175. A185：Display Alias与歧义关闭

唯一`display_name`可作为人类可读run alias；同alias命中多个run，或同一target同时命中run、saved Capsule、builtin时返回`ambiguous`，不采用隐式优先级。直接执行Capsule仍保持A174 builtin-first，但“解析用户所指对象”和“执行已明确类型”是两个不同安全边界。

## 176. A186：Command-only Invocation Policy

对齐InfCodeX ADR-047：Host只对显式command返回`suggest`，natural-language始终为`none`。原因是Host尚未调查repo，关键词或复杂度正则会在真实上下文出现前抢跑并生成浅层流程；自然语言是否需要多Agent由拥有工具和上下文的Agent判断。

## 177. A187：Start Outcome Turn-consumption

固化`started/cancelled`消费当前turn，`declined/failed`不消费的合同，并通过`workflow_host`只读工具投影。这样终端或SDK consumer不必分别猜测“拒绝生成”和“用户取消已进入交互”的后续输入处理。

## 178. A188：Manifest/Host/System Min-wins Limits

参考InfCodeX `clampWorkflowLimits()`实现Agent、并发和token三类有效上限。Agent/并发非法正数保守归1并受system cap约束；token的0、负数、非有限值表示未声明上限，不会误成1-token预算；manifest、host与system提供的有效值取最小。NZ执行时system上限进一步取当前Session Manager的真实容量。

## 179. A189：Pre-run Approval Summary

`build_workflow_approval_summary()`稳定输出name、description、phase、planned agents、三类effective limit和`writes_files`。摘要与执行共享同一clamp函数，避免UI展示8并发但runtime按4执行之类的双重事实。Headless未提供approval callback时仍自动执行，与InfCodeX一致；摘要不伪装成已获得用户批准。

## 180. A190：Host Ceilings进入真实Runtime

`workflow_run(host_policy=...)`在首个effect前以worst-case Agent count拒绝超过effective maxAgents的计划；Runtime并行池同时取phase、host和Manager最小值；effective token budget进入既有spawn前门禁。专项分别证明零spawn拒绝、请求4并发但真实峰值1，以及7-token Child后第二个Agent不启动。

## 181. A191：Display Name持久消费链

可选1–200 printable字符的`display_name`进入workflow started event、managed run name、outcome、terminal `run.json`和`workflow_runs list`摘要；控制字符在创建任何run前拒绝。内部唯一身份仍是随机run ID，alias只用于显示和明确解析，不能替代持久主键。

## 182. A192：Identity-aware Resume Target

`workflow_run(resume_target=...)`接受精确run ID或唯一display alias，解析为run ID后复用A157 content-addressed successful-result cache。saved/builtin/missing/ambiguous target一律在新run创建前拒绝；`resume_from`与`resume_target`互斥。端到端测试证明alias resume重放1个成功Child且不新增spawn。

## 183. A193：Scout-then-author Host API

参考InfCodeX `author-via-worker.ts`建立单一`SCOUT_THEN_AUTHOR_PROMPT_LINES`和builder，并由`workflow_host(action=author-prompt)`投影。指令要求当前Agent先用自身工具找到exact paths、具体子问题和真实output schema，再调用现有`workflow_run`；NZ不增加另一个Worker Session或Runner，因为当前AgentRuntimeAssembly已是唯一控制面。机器contract升级到1.4，声明invocation、turn、limits、identity、resume和authoring语义。

本轮14项新增专项、95项Workflow/Host/Manager核心组合与1296项完整回归全部通过，另deselect 1项既有live Bash测试；Ruff、`git diff --check`和真实工具注册冒烟通过。唯一pytest warning是既有Python 3.13 multiprocessing fork deprecation，Conda matplotlib `.pth`启动traceback仍为环境噪声。未运行SWE-bench、付费Provider或公网MCP。剩余差距包括真正终端`/workflow` approval renderer、跨进程active-run identity、SDK异步first-workflow-started promise，以及公网模型是否稳定遵守scout-then-author/structured output；本地fixture不能证明这些外部互操作能力。

## 184. A194：Approval Summary Digest

参考InfCodeX approval callback边界，为A189 effective summary增加canonical JSON SHA-256。Digest覆盖limit、phase与write risk，成为UI决策所批准对象的稳定身份；字典顺序变化不改变digest，任何有效字段变化都会改变。

## 185. A195：Stale Approval关闭式拒绝

`evaluate_workflow_approval()`接收UI看到的expected digest；与执行前重算值不符时返回`failed/stale approval summary`，不创建run、不发布Child。这样审批窗口打开后计划或Host policy变化不能沿用旧“同意”。

## 186. A196：Typed Approval Outcome Gate

approve→started、deny→declined、cancel→cancelled、缺省交互→pending，且复用A187 turn-consumption合同。`workflow_run`在完整preflight后、run ID分配前执行gate；deny/cancel只返回结构化receipt与summary。

## 187. A197：Headless Auto-approval Receipt

InfCodeX无approval callback时headless自动继续；NZ保持该兼容行为，但写入`mode=headless-auto`、decision、digest和outcome到start event、outcome与run.json，避免“没有用户交互”和“用户批准”混成同一事实。

## 188. A198：Terminal Run Rename

新增`workflow_run_rename`写工具。只接受1–200 printable alias，读取并验证精确terminal record后原子重写，run ID不变，并发布`workflow_run_renamed`事件。Identity随后可用新alias解析；控制字符或未知run不产生写入。

## 189. A199：Saved Workflow Atomic Rename

`rename_workflow_capsule()`在明确project/personal scope内解析普通非symlink文件，验证旧Capsule，拒绝已存在目标，再用`os.replace`原子rename。它不依赖project覆盖顺序猜测用户要改哪一层。

## 190. A200：Saved Workflow Recoverable Delete

`workflow_library_mutate(action=delete)`要求`confirm=true`，随后把精确Capsule移动到同目录私有0700 `.trash`并返回恢复路径，不做不可逆删除。该语义与run archive保持一致。

## 191. A201：Saved Workflow Revision-preserving Replace

Replacement先完整validate并执行1MiB限制，再把旧validated Capsule以0600写入私有`.history`，最后复用atomic save覆盖主文件。返回previous revision路径；无效新Capsule不会触碰旧主文件。

## 192. A202：Bounded Result Summary

terminal writer从最后phase的string/dict/list递归提取可展示文本，截断到20K写入`result_summary`。`workflow_runs(action=result)`提供安全reader；无结果时显式not found，不把整个raw Child state当摘要泄露。

## 193. A203：Retention Dry-run

`workflow_run_archive(dry_run=true)`允许不带confirm执行所有active/unknown/record安全预检，只返回candidate IDs且不移动任何目录。真实archive仍要求confirm并保持recoverable trash。

## 194. A204：Active与Persisted History Union

`workflow_runs(list)`先投影Manager managed snapshots，再追加未重复的terminal run records并统一limit。active/paused状态不再因尚未有run.json而从历史面消失；同run不会同时出现两行。

## 195. A205：Generation JSON Extraction

参考InfCodeX generator的plain/fenced/surrounded JSON兼容边界新增`workflow_generation.py`。只提取首个外层`{...}`候选；没有对象或JSON损坏确定性报错，不在自由文本里猜字段。

## 196. A206：Typed Decline/Generate Envelope

Decline必须提供非空reason；generate必须提供approval summary，并接受validated Capsule或pattern/request/options后生成的JSON-only Capsule。任何未知action、错误options或Capsule schema在进入library/runtime前拒绝。

## 197. A207：Generation Timeout Contract

Timeout优先级为显式seconds、`NZ_WORKFLOW_GENERATION_TIMEOUT_SEC`、legacy毫秒环境、120秒默认；非法/非正值回默认，硬上限600秒。这里定义Host调用预算，不启动Provider或后台timer。

## 198. A208：Two-attempt Generation Repair

生成错误的修复提示要求只返回decline/generate JSON并明确禁止source，错误与旧输出分别有2K/4K边界。`next_workflow_generation_repair()`强制最多2次，第三次返回`allowed=false`而不是继续循环。

## 199. A209：Generation Tool Consumer

新增只读`workflow_generation`工具统一parse、timeout与repair-prompt。Tool返回validated Capsule metadata，不能借生成协议写文件或执行代码；真正保存/执行仍经过已有write/serial工具与权限链。

## 200. A210：Main Agent Conservative Tool-name Repair

参考InfCodeX `tool-name-repair.ts`实现仅大小写和`_/-/空白`归一后的唯一匹配；合法名、不匹配和多候选碰撞不改。Main Loop在history、Session parts、scheduler和dispatch任何consumer之前统一重写，并记录from/to/call ID trace。

## 201. A211：Child Agent Tool-name Repair

Subagent在Provider message落盘前使用其实际暴露tools集合执行同一唯一匹配规则。真实Child测试让模型调用`Read-File`，证明第二次Provider请求收到`read_file`成功结果；不是只测纯函数。

## 202. A212：Structured Tool-result Classification

新增统一error/cancel/code predicates，兼容InfCodeX bracket envelope与NZ现有`Error:/Denied/Cancelled`。ToolExecutor据此设置dispatch_failed、cancelled metadata并提取`OLD_TEXT_NOT_FOUND`类大写code；Bash非零仍保持command feedback而非dispatch failure。

## 203. A213：Provider Retry与Terminal Diagnostics

`describe_transient_provider_retry()`区分stream incomplete/stall、hard/request timeout、connection和abort；Loop API error trace消费该标签。`<promise>COMPLETE|BLOCKED|DECIDE[:reason]</promise>`由同一resilience模块提取并进入LLM terminal observation，不改变模型自然结束判定。

本轮新增28项专项、239项Workflow/Agent/Tool核心组合与1324项完整回归全部通过，另deselect 1项既有live Bash测试；Ruff、`git diff --check`和真实工具dispatch冒烟通过。唯一pytest warning是既有Python 3.13 multiprocessing fork deprecation，Conda matplotlib `.pth`启动traceback仍为环境噪声。机器contract升级到1.5；未运行SWE-bench、付费Provider或公网MCP。剩余差距是终端approval renderer/异步SDK callback、生成Provider调用编排、non-streaming fallback独立Attempt Controller，以及公网模型互操作证据；当前实现没有把纯协议函数冒充这些外部闭环。

## 204. A214：Provider Attempt Controller与Stream Watchdog

### 204.1 InfCodeX参考能力

参考`packages/coding/src/agent-runtime/non-streaming-fallback.ts`、`provider-retry-policy.ts`和`stream-timers.ts`。关键不是多写一个retry函数，而是由每次模型调用的单一owner判断失败发生在稳定边界之前还是之后，并分别管理streaming attempt、buffered fallback、idle timer与hard timer。

### 204.2 NZ-Coder原有不足

A213只有错误分类标签。`_call_streaming()`遇到异常会直接进入通用backoff；如果Provider迭代器不再产生chunk，当前线程也无法观察取消或超时。已有“模型声明不支持stream时改用non-streaming”不等于运行时stream失败恢复。

### 204.3 实现结果

`ProviderAttemptController`只在retryable stream错误、没有text/reasoning/tool稳定边界、且本轮尚未fallback时选择一次`non_streaming_fallback`。fallback使用独立一次调用，不递归拥有另一套retry loop；失败才回到既有RecoveryState。`_iter_completion_with_timeouts()`用单chunk握手保持Provider流消费顺序，idle/hard watchdog有界等待，并每100ms观察attempt retirement。因而tool-call finish仍先执行工具，再请求trailing usage chunk，不因预取改变语义。

### 204.4 关键文件与验证

- `nz_coder/runtime/agent_resilience.py`：attempt decision与稳定边界策略。
- `nz_coder/runtime/loop.py`：真实stream consumer、watchdog、fallback consumer和trace。
- `nz_coder/config.py`、`.env.example`：retry、idle、hard和fallback配置。

专项覆盖单次fallback、稳定边界禁止fallback、idle timeout、取消和stream→buffered真实Provider shape；完整回归1333 passed、1项已知Conda `.pth`污染Bash精确输出测试deselect、1项既有fork warning。未向公网Provider发送请求。

### 204.5 剩余差距

Python同步SDK没有通用AbortSignal；watchdog会close stream并让Agent继续，但无法保证第三方SDK内部不合作的socket线程立即消失。官方OpenAI/native transport自身timeout仍是第二道边界，公网矩阵需显式live smoke证明。

## 205. A215：Workflow Provider生成编排

### 205.1 参考与原有不足

参考InfCodeX `workflows/generator.ts`的text-only Provider调用、严格JSON gate、统一deadline和两次repair。NZ的A205–A209已经有extract/parse/timeout/repair函数，却没有任何owner真正调用Provider，因此只能验证别人提供的raw text。

### 205.2 实现结果

`generate_workflow_with_provider()`用当前Provider进行无工具text call，简单任务可decline，复杂任务只能选择六种已声明pattern或提交经过完整validator的inert Capsule。初次调用和最多两次repair共用一个deadline；超时、第三次非法输出和可执行source形状全部关闭式失败。`AgentLoop.generate_workflow()`成为生产consumer，终端`/workflow generate`调用它，而不是让模型输出Python/JavaScript后执行。

### 205.3 验证与边界

专项证明非法首答被repair为有效Capsule，以及整个生成链超时。公网模型遵循JSON的稳定率尚未实测；这属于互操作证据，不再是代码缺口。

## 206. A216：终端Workflow审批与命令消费链

### 206.1 参考与原有不足

参考InfCodeX REPL workflow-command builder的“先显示真实effective summary，再由Host callback批准”边界。A194–A197已有digest、stale gate与typed outcome，但终端没有renderer，Agent工具调用会走headless-auto。

### 206.2 实现结果

新增execution-local `scoped_workflow_approval_asker`。Agent worker调用`workflow_run`时，通过现有线程安全TerminalInteractionBridge在唯一prompt_toolkit Application展示name、description、phases、planned agents、concurrency和write risk；返回approve/deny/cancel后绑定当前summary digest。`/workflow`进入统一command registry与slash completion，支持list/show/run/generate/pause/resume/stop。command路径显式审批，不能在缺少交互channel时静默启动。

### 206.3 验证与边界

终端/Workflow组合225项通过；真实PTY启动安装态`nz-coder`，输入`/workflow list`得到`No workflow runs.`，再`exit`正常退出。没有实际启动付费多Agent workflow。

## 207. A217：异步Workflow Host SDK

### 207.1 参考与原有不足

参考InfCodeX managed workflow start返回run identity，并提供first-workflow-started promise。NZ原`workflow_run`同步执行到所有Child结束，若由slash command直接调用会冻结composer。

### 207.2 实现结果

`WorkflowHostSDK.start()`预分配安全run ID并返回`WorkflowRunHandle`。Handle同时提供阻塞/async first-started和terminal result、pause/resume/stop；执行线程复制当前Context并重新绑定workspace与Session manager。first-started只在durable `workflow_run_started`已经写入后resolve；preflight或approval失败则以`WorkflowStartError`拒绝，不伪造run started。

### 207.3 验证与边界

真实fake-child运行证明started先于terminal、同一run ID贯穿handle、manager和run record。SDK当前是进程内Python Host API；HTTP/IDE没有真实consumer时不扩建第二套远程Workflow API。

## 208. A218：Workflow跨进程Identity恢复

### 208.1 参考与原有不足

进程内`_managed_runs`退出即丢失；虽然terminal record与event journal存在，新Manager无法展示上个进程尚未结算的run identity。

### 208.2 实现结果

`WorkflowProcessStore.workflow_run_lifecycles()`从append-only事件重放started/paused/resumed/completed/failed/stopped。Manager初始化时恢复terminal identity；没有terminal事件的running/paused run不能假装可继续，追加带`recovered=true`的failed事件并标记`workflow interrupted by process restart`。这是关闭式恢复，而不是伪造线程/Provider可跨进程续跑。

### 208.3 验证与边界

专项构造仅有started事件的旧进程状态，新Manager同时在snapshot和durable lifecycle中得到failed。真正跨机器租约和恢复Child执行不属于当前单机终端目标。

## 209. A219：Provider与MCP互操作验证入口

### 209.1 实现结果

主CLI新增`provider-smoke`，复用既有text/tool-result/stream完整round-trip；MCP CLI新增`smoke SERVER`，验证连接、initialize/capability enumeration，并可按明确tool+JSON arguments调用一个工具。两个入口缺省均dry-run，只有`--confirm-live`才进行可能计费或访问外部服务的动作，输出不包含credential和response body。

### 209.2 验证与边界

真实命令`nz-coder provider-smoke --checks text`与`nz-coder mcp smoke example`均确认没有发请求的dry-run合同。当前用户未授权公网请求，因此不能把入口存在写成公网兼容已证明。

## 210. A220：SWE-bench可复现身份清单

### 210.1 实现结果

主CLI新增`nz-coder swebench`路由到唯一官方helper。每次`run-agent`在首个实例前写`.manifest.json`，记录dataset/split、精确instance IDs、Provider/model、turn/timeout、Python/platform、NZ版本和无需Git的安装源码SHA-256；不记录API key。manifest明确first-pass与retry必须分开，防止人工retry混入首轮口径。

### 210.2 验证与边界

manifest原子0600写入和secret-free字段有专项测试；`nz-coder swebench --help`真实命令通过。按用户此前要求，本轮没有运行300实例、Docker harness或付费Provider，所以分数仍然未知，不能声称与InfCodeX相同。

本轮最终验证：9项闭环专项、225项Workflow/CLI/MCP/SWE组合、1333项完整回归通过；另deselect 1项被当前Conda matplotlib `.pth`启动traceback污染的既有Bash精确输出测试，保留1项Python 3.13 multiprocessing fork warning。Ruff、`git diff --check`、安装态CLI dry-run和真实PTY通过。机器contract升级到1.6。

## 211. A221：真实Provider、终端与SWE小样本证据闭环

### 211.1 原有证据不足

A219/A220只证明验证入口存在，不能证明配置中的DeepSeek、真实终端Agent工具链或官方Docker harness可工作。原全量测试又被一个指向已删除SWE工作树的Conda matplotlib editable `.pth`污染，导致Bash精确输出用例失败。

### 211.2 实现与环境收口

- 将两份失效matplotlib `.pth`移动到`site-packages/nzcoder-stale-pth-backup-20260809/`，保留可恢复副本；Python启动和原失败Bash测试恢复干净输出。
- `pyproject.toml`增加`dev` extra，固定`ruff==0.15.10`和兼容pytest范围；发布清单统一使用`python -m pip install -e ".[dev]"`，避免Ruff默认规则随版本漂移。
- 保持A220的first-pass规则：固定数据集顺序前10题，不人工选择成功题、不混入retry；manifest记录10个instance ID、模型、Provider、源码哈希和运行参数。

### 211.3 真实验证结果

- DeepSeek Provider：text、tool call/result round-trip、stream三项实网通过。
- 真实PTY：Agent调用`read_file`读取README，回答后双Ctrl+C退出码0。
- 发布wheel/真实PTY smoke通过；最终Python 3.13全量`1335 passed`。
- SWE-bench Lite首轮10题：10个非空patch，官方Docker harness得到`6 resolved / 4 unresolved / 0 errors`，即60%。resolved为`astropy-12907/14995/6938`、`django-10914/10924/11001`。

### 211.4 失败分类与剩余差距

- `astropy-14182`：只调整RST writer构造，没有完整处理多header row读取，dtype行仍被当数据；属于patch不完整。
- `astropy-14365`：正则识别了小写`no`，数据转换仍只判断大写`NO`；质量门已提前标记case-normalization风险，属于跨调用点修复不完整。
- `astropy-7746`：局部空数组验证通过，但官方目标调用路径仍失败；属于验证目标不精确。
- `django-11019`：直接参考上游PR后改动过宽并修改测试，破坏多项既有media顺序语义；属于回归破坏，质量门已标记tests_modified。

该10题样本只能证明真实闭环和暴露失败模式，不能外推300题分数或宣称与InfCodeX榜单相同。真实第三方MCP仍因未配置server而没有公网证据。

## 211A. A222：SWE-bench Verified严格主榜流程

### 211A.1 为什么原流程不能继续作为主榜口径

A221的Lite前10题使用了会注入`hints_text`的prompt，Agent还能调用`webfetch`；其中`django__django-11019`实际查询了上游GitHub PR。它证明本地Agent、Provider和Docker harness能够闭环，但不符合官方pass@1无答案泄漏要求。`retry-agent`又会读取FAIL_TO_PASS/PASS_TO_PASS，任何这类结果都只能用于诊断，不能混入正式预测。

### 211A.2 新的主榜合同

- `verified`固定到`princeton-nlp/SWE-bench_Verified`的500个test实例，是唯一主榜profile；`lite`固定300题，只做开发冒烟。
- prompt只读取公开problem statement及仓库/base commit身份，完全忽略hints、gold patch和官方测试字段。
- 每题只允许一次Agent调用；空patch直接作为空预测，不追加提示重跑。append-only journal在setup/Agent前先持久`claim`，结束后再写`result`；崩溃恢复永不重新启动已claim的题。
- Agent模型可见工具采用本地白名单；webfetch、MCP、memory、skill、workflow和child Agent不进入schema且dispatch二次拒绝，planning/reflection与memory读写也在strict Context关闭。Bash只接受小型本地命令语法，不依赖可绕过的联网denylist；checkout随后重建单commit Git snapshot，base之后的gold history不可读。
- 原始trace在推理完成时导出脱敏公开JSONL；提交validator检查500个唯一prediction、manifest pass@1/no-leak声明、每题轨迹、轨迹工具白名单和官方report/test output。最终bundle统一生成`all_preds.jsonl`、`metadata.yaml`、`README.md`、`manifest.json`、`trajs/`与规范化`logs/`，其中`patch.diff`由对应prediction生成。
- `run-eval`完整成功后默认尝试自动打包；任何缺题、重复ID、缺轨迹/日志、禁用工具或诊断manifest都会fail closed。

### 211A.3 模型与验证边界

默认模型和首装模板已切换为`deepseek-v4-flash`，endpoint为`https://api.deepseek.com`；workspace环境变量仍具有最高优先级。本阶段只运行本地契约、兼容性、静态编译和CLI检查，不发起500题计费推理，也不伪造主榜分数。官方在2025-11-18后对Verified公开提交另有开放研究成果、合资格机构和开源方法要求；本地官方harness通过不等于上游PR必然获接收。

## 211B. A223：运行中Trace反查InfCodeX/InfCode真实处理链

### 211B.1 触发原因与证据

严格Lite r3运行到112个raw trace诊断包时，流程审计发现28个`agent_failed`中有22个
实际已经在子进程trace写出`run_end=completed`并留下非空patch；另有大量strict Bash
拒绝、首次编辑前长期搜索和验证成功后继续探索。该问题不能再用“对应模块已经存在”判断
对齐程度，必须回到InfCodeX与公司内`infcode-dev`当前生产源码核对真实owner和终态链。

### 211B.2 两个参考实现如何处理

- **评测子进程结果传输（InfCodeX）**：`benchmark/harness/agent-task-runner.ts`
  在子进程运行期间持续消费stdout/stderr，timeout只负责硬终止；完整Session JSONL独立落盘，
  返回对象只保留有界64KB尾部和typed status。它不会先等待子进程退出、再读取一个可能写满
  pipe的批量Queue。因此NZ当前`join(timeout) -> queue.get()`不是参考实现的行为。
- **Agent loop owner（两者）**：InfCodeX `Runner`和InfCode `SessionPrompt`都在同一异步
  runtime内驱动LLM/tool状态机；大轨迹由Session持久层承担，不作为进程返回payload。
  InfCode又以Assistant `finish`、是否存在未处理ToolPart和parent User ID共同决定是否继续，
  并为provider缺失finish reason持久化`unknown`，避免无终态无限续轮。
- **防循环（InfCode）**：`SessionProcessor`只在最近三次ToolPart的tool name和JSON input
  完全相同时触发`doom_loop` permission。它不识别“不断换关键词/文件但没有形成修改”的
  语义游走。
- **防循环（InfCodeX）**：L1在20调用窗口内检查同一`tool+stable input`三次重复，或有
  read-cache hit后的两次重复；L2异步Sidecar读取最近transcript，判断是否真的无进展，若
  stuck则在下一次`beforeTool`拦截调用并注入具体nudge。它比InfCode/NZ的精确三连更准确，
  但L1仍以相同参数为触发条件，不能单独覆盖r3中大量“参数变化、目标不收敛”的轨迹。
- **终止与返工（InfCodeX）**：Runner采用min-wins迭代上限；自然文本终态经过stop hook，
  Sidecar Verifier可返回accept/revise/blocked，revise最多reanimate两次。工具结果还支持
  `isTerminal:true`直接结束，不必再让模型决定是否继续。InfCode主要依靠finish reason和
  max-step文本提醒，没有同构的终端验证工具信号。
- **Shell协议（InfCode）**：Bash schema直接提供`workdir`并明确要求不要使用`cd`；使用
  Tree-sitter解析command/path/redirection，再按`allow/deny/ask`通配规则和外部目录权限
  决策，而不是让模型反复猜一个隐藏的小型语法。
- **Shell协议（InfCodeX）**：普通产品模式把命令分成safe/normal/dangerous，并通过
  tri-state before-tool gate处理；长输出持续drain到有界collector/独立artifact。
  这些规则允许`git log`等命令，不能直接复制到禁止history/联网答案的SWE strict模式。
- **结构化代码理解（InfCodeX）**：Worker prompt不仅列工具名，还写出明确触发条件：准备
  读取3个以上同模块文件先用`module_context`，符号调用先用`symbol_context`，影响分析先用
  `impact_estimate`。其源码注释记录该段对DeepSeek/Kimi/Minimax等弱工具选择模型带来
  30--40pp first-tool采用提升。NZ只暴露schema/通用提示，r3实际仍以grep/read为主。

### 211B.3 对NZ当前实现的真实判断

1. **确定P0假超时**：`_run_agent_attempt_worker()`将每条最多4,000字符的全部工具日志一次性
   `queue.put()`，父进程却先`process.join(900)`；约64KB以上发生pipe backpressure后双方
   互等。22个已完成patch随后又被`agent_failed`分支清空。这是运行器bug，不是模型失败。
2. **已有stop-hook只完成了机制，没有完成主Agent消费链**：`runtime/hooks.py`已经有
   InfCodeX式三态与两次reanimate预算，但生产Main Agent没有注册Sidecar stop hook；现有
   fresh sidecar主要位于Workflow验证路径。不能再把“hook类存在”表述成Main Agent已对齐。
3. **精确doom-loop与上游InfCode基本同级，不足以解决本次语义游走**：r3失败组首次写入前
   调查调用中位数34，4题在48--61次调用后仍无写入；这些调用持续改变参数，三连检测不会
   触发。InfCodeX sidecar思想可借，但需要增加“有界调查窗口内无mutation/无新阶段证据”
   的L1触发，不能声称这是InfCode原样能力。
4. **strict策略需要保持fail-closed，但必须成为模型可见协议**：不能照搬两仓普通产品的
   permissive Bash；应借InfCode的显式`workdir`和InfCodeX的结构化分类/有界输出思想，向
   模型给出精确允许语法与可执行替代命令，并把过程policy error与patch semantic risk分开。
5. **验证后停止应使用确定性runtime终态**：借InfCodeX terminal tool signal，在strict
   条件满足“非空source diff + `verify_changed_files`通过 + 无未结算写/工具批次”时由runtime
   收口，而不是仅靠prompt第7条要求模型自行停止。普通终端产品仍保留自然finish与可选
   Sidecar Verifier，避免把SWE特定策略扩散到交互产品。

### 211B.4 建议实现顺序与验证标准

1. 先修子进程协议：持续drain事件或先收typed result再join；大trace只落盘，IPC只传有界
   status/计数。故障测试必须覆盖>64KB工具日志、Agent已完成、异常、真timeout和子进程崩溃。
2. 再补strict `bash.workdir`、模型可见command grammar和结构化拒绝建议；保持联网/history
   fail-closed，并用r3已归档443次Bash error做离线重放分类。
3. 增加phase-progress detector：调查调用预算、首次编辑deadline、mutation generation、
   verification generation与policy-error streak作为结构化状态；先确定性nudge，再以有界次数
   stop/降级，避免默认增加Sidecar费用。
4. 将成功strict验证接到terminal tool signal；同时为普通Main Agent显式装配可配置
   Sidecar stop hook，分别测试accept/revise/blocked/timeout fail-open与两次返工上限。
5. 把`repo_map/read_symbol/analyze_impact`的触发条件写入production真实tool description/prompt，
   用InfCodeX EVAL_GUIDELINES的Layer 2 single-turn probe验证DeepSeek首工具选择；不直接用继续
   跑300题来比较prompt效果。

本节是源码复核和修复合同，尚未实施上述改动，也没有运行新的付费eval或官方Docker
harness。详细r3统计同时记录在`swebench-progress.md`的“运行中Trace流程审计”。

## 211C. A224：SWE strict运行器与Agent阶段收敛

### 211C.1 InfCode/InfCodeX参考能力

- 参考文件：`references/InfCodeX/benchmark/harness/agent-task-runner.ts`、
  `references/InfCodeX/packages/agent/src/primitives/runner.ts`、
  `references/InfCodeX/packages/coding/src/multi-instance/stall-detector.ts`、
  `infcode-dev/infcode-dev/packages/opencode/src/tool/bash.ts`。
- 核心行为：进程输出持续drain、完整轨迹与有界返回值分离；Bash显式`workdir`；
  工具阶段可由terminal signal直接结束；重复/停滞检测在工具执行前给出模型可见反馈。

### 211C.2 NZ-Coder原有不足

- 父进程`join(timeout)`早于Queue读取，工具事件超过pipe buffer后把已完成Agent误判成900秒超时。
- strict Bash规则只存在于dispatch侧，模型看不到精确语法，也没有`workdir`替代`cd`。
- RuntimeState虽然会提示“长时间未编辑”，但变化参数的读/搜没有硬上限。
- `verify_changed_files`成功只更新验证状态，仍需模型再决定是否停止，长轨迹会重新探索。
- strict policy拒绝与真实写入/执行错误共同进入`tool_errors`，使patch risk口径混杂。

### 211C.3 实现结果

- 子进程协议改为父进程先按deadline接收typed payload，再join已排空的child；不再调用不可靠的
  `Queue.empty()`。每条跨进程工具摘要限制为512字符，完整工具输出仍由trace负责。
- `bash`新增可选`workdir`，同时支持workspace内相对/绝对目录；不存在、非目录或路径逃逸均以
  `Error:`返回且不会启动shell。strict拒绝会给出`bash.workdir`、允许Git形式或允许
  `python3 -m`验证形式。
- strict system prompt列出实际允许的shell grammar，并给出`repo_map`、`read_symbol`、
  `find_symbol_callers`、`code_references`和`analyze_impact`的具体触发条件。
- RuntimeState按mutation generation统计成功调查调用；12次时只注入一次确定性收敛nudge，
  20次时在dispatch前阻止继续read/search/navigation。同批工具也按顺序预留预算；编辑、diff、
  verify和最终文本不受阻。
- strict条件下，`diff_status`确认非空source-only diff且`verify_changed_files`返回`OK:`后，
  当前工具批次完整结算并直接返回现有terminal action，不再追加Provider请求。
- strict policy rejection单独记为`policy_rejected`/`process_warnings`，不再自动生成补丁语义
  `tool_errors`标签；真实Bash路径、dispatch和写工具错误仍保留风险标签。

### 211C.4 关键设计决策

- 没有把全部trace从子进程搬回父进程：这会重新制造IPC背压；磁盘trace才是完整事实源。
- 没有照搬InfCodeX普通产品的宽松safe/normal/dangerous命令表：SWE严格模式仍禁止联网和
  Git history，只借鉴显式协议与结构化反馈。
- 没有默认调用额外Sidecar模型判断停滞：r3的主要问题可由mutation generation和固定预算
  离线确定，避免新增费用和第二个不稳定判断源。
- terminal只作用于strict且要求source-only diff；普通终端Agent、失败验证、测试文件修改和
  非源码diff仍沿用自然finish/verification gate。

### 211C.5 关键文件

- `nz_coder/swebench/orchestrator.py`：有界IPC协议、strict prompt和过程诊断分类。
- `nz_coder/swebench/policy.py`：strict Bash拒绝后的可执行改写建议。
- `nz_coder/tools/bash.py`：workspace内`workdir`解析和schema。
- `nz_coder/runtime/runtime_state.py`：调查预算、mutation generation和source-only状态。
- `nz_coder/runtime/loop.py`：strict progress dispatch gate与验证terminal consumer。
- `tests/test_swebench_lite.py`、`tests/test_swebench_strict.py`、`tests/test_bash_progress.py`、
  `tests/test_runtime_state.py`、`tests/test_loop_fake.py`：故障与闭环测试。

### 211C.6 验证结果

- 静态检查：5个修改后的生产模块`py_compile`通过；相关生产/测试文件Ruff通过。
- 定向测试：>64KB跨进程工具日志、异常payload、`exitcode=7`无payload、真实timeout、
  Bash workdir/逃逸、strict改写建议、12/20预算、同批预算和terminal正反路径均通过。
- 聚焦回归：227 passed。
- 完整回归：1394 passed，唯一warning为Python 3.13对多线程进程中`fork()`的上游弃用提示。
- 真实冒烟：真实Git临时仓库中的edit→diff_status→verify_changed_files链在strict模式下由
  3次Provider响应结束；旧实现同一测试会继续到5次。
- 是否运行评测：没有恢复Lite/Verified推理，也没有发起付费Provider或官方Docker harness。

### 211C.7 学习重点

1. Agent trace显示`completed`并不代表父进程能收到结果；进程终态协议必须同时验证数据流和生命周期。
2. fail-closed策略若不是模型可见协议，会把安全性成本转化为大量无效重试。
3. 精确重复检测与阶段无进展检测解决的是两类问题；后者必须绑定mutation generation。
4. 验证工具若没有runtime consumer，就只是提示能力；接入terminal后才形成确定性闭环。

### 211C.8 剩余差距

- 新策略只做了离线和本地闭环验证，尚未用新的独立Lite样本比较首写调用数、总工具数和patch质量。
- Python 3.13在多线程父进程使用`fork`仍有弃用warning；后续可评估`spawn`协议，但不能在没有
  picklability/trace owner测试前直接切换。
- 12/20阈值来自r3分布与InfCodeX 20-call window，应在不计榜的小规模独立样本上校准，不能视为固定最优值。
- terminal当前依赖Agent先调用`diff_status`；若未来要自动读取Git状态，应由单一owner实现，避免第二事实源。

## 211D. A225：A224后20题真实Trace审计与安全续跑上限

### 211D.1 InfCode/InfCodeX参考能力

- 本阶段不新增上游模块映射，而是用A224已经借鉴的InfCode Bash显式协议、InfCodeX
  terminal signal、stall detector和有界runner语义检查NZ-Coder真实运行效果。
- 核心审计原则：运行生命周期、Agent推理、仓库准备和patch质量必须分层归因；不能把clone失败
  算成模型失败，也不能把最终已恢复的过程错误永久算成patch语义风险。

### 211D.2 NZ-Coder原有不足

- r3的manifest绑定修复前源码指纹，A224后无法也不应在原run中混合不同实现版本。
- `--max-instances`限制的是输入集合，resume跳过旧claim后不能表达“本次只新增20个持久结果”。
- A224的本地测试不能证明真实模型会遵守结构化调查工具、Bash grammar和验证事件顺序。

### 211D.3 实现与审计结果

- `RetryOrchestrator.run_batch()`增加`max_new_instances`。旧claim不计数；每个新实例只有在结果、
  journal、prediction、公开trajectory、raw trace归档和可选worktree清理完成后才触发上限判断。
- CLI增加`--max-new-instances`，本次以20运行并自动输出`[PAUSE]`后停止，没有后台残留进程。
- 为避免污染旧r3，建立独立续片`lite20-dsv4flash-20260810-r3-cont-a224`，只选择其后20个
  未尝试实例；这不是重跑，也不是正式pass@1成绩。
- 20条结果为15 completed、3 risky、2 setup_failed。18个进入Agent的实例均有非空patch，
  raw trace均以completed终态结束，且没有API error、Provider retry、context compaction或假timeout。
- 两个setup_failed分别来自Git clone的GnuTLS decode/early EOF与TLS异常关闭，未进入Agent。
- 真实trace确认六项剩余问题：只读Bash绕过调查预算；hard gate反馈可重复；diff/verify终态仍受
  调用顺序影响；risk累计已恢复历史错误；strict Bash grammar产生50次拒绝且`rg`不可用；
  `repo_map`/`code_references`/`analyze_impact`仍未进入主路径。

### 211D.4 关键设计决策

- 没有修改旧manifest或伪造源码指纹，因为那会破坏run可复现性和证据边界。
- 自动暂停放在完整持久化与清理之后，而不是Agent刚返回时，避免第20题只留下半份证据。
- 没有因达到20题就清理trace：续片约2.0GB，总目录约14GB，尚未达到用户设定的20GB分析阈值。
- 本阶段只诊断并记录根因，不用另外的付费样本反复试错，也不把预测文件当官方harness成绩。

### 211D.5 关键文件

- `nz_coder/swebench/orchestrator.py`：按新增持久结果计数并安全暂停。
- `nz_coder/swebench/cli.py`：公开`--max-new-instances`运行参数。
- `tests/test_swebench_strict.py`：证明resume跳过项不计数且未领取第N+1个实例。
- `.nz-coder/swebench-lite/predictions-lite20-dsv4flash-20260810-r3-cont-a224.report.json`：
  20题诊断结果事实源。

### 211D.6 验证结果

- 定向测试：新增resume/durable limit测试先失败后通过；SWE strict/Lite相关80项回归通过。
- 真实运行：20条prediction、40条claim/result journal、20份公开trajectory、18份Agent raw trace；
  达到20个新增持久结果后自动暂停，进程已退出。
- 存储：本续片raw trace约2.0GB，总SWE目录约14GB，未触发20GB清理策略。
- 是否运行评测：运行的是不计榜的Agent流程诊断；未运行官方Docker harness，不报告resolved率。

### 211D.7 学习重点

1. resume安全不仅是“不重复instance”，还必须把源码/config指纹和每次调用的新增额度分开建模。
2. 工具名预算不是语义预算；只要Bash能完成同类读取，模型就能绕开按工具名实现的阶段门。
3. terminal与risk都应由mutation generation的最终证据结算，而不是依赖调用顺序或累积全部历史错误。
4. 真实环境中的命令可用性属于Agent协议；prompt推荐一个不存在的`rg`会稳定制造无效调用。

### 211D.8 剩余差距

- 将安全只读Bash纳入调查预算，同时避免把process/status/diff类Bash误算为调查。
- 同一generation累计diff与成功verification，不论调用顺序都应进入确定性terminal。
- 过程错误按generation结算；已修复且最终验证通过的错误只保留诊断，不污染patch semantic risk。
- hard gate达到有界次数后只允许edit/diff/verify/final blocker，不能无限重复拒绝。
- 提升结构化代码工具的真实采用率，并让strict命令提示基于实际command availability生成。
- Git clone增加可审计的缓存/有限重试后，才能减少外部TLS故障造成的样本损失。

## 211E. A226：Agent Core生产链源码级纠偏（trace_verified）

### 强制对齐门

本阶段纠正A224的错误完成声明。此后“源码级对齐”必须同时给出上游参考、producer、状态owner、
持久/恢复边界、production consumer、contract test和真实trace。状态固定为`mechanism_only`、
`wired`、`contract_verified`、`trace_verified`四级；只有最后一级允许写“完成”。

### 已实现并接入生产链

- `RuntimeState`记录`diff_generation`与`verification_generation`；同一mutation generation中
  verify先于diff也能terminal，新mutation自动使旧证据失效。
- SWE最终risk只消费最终generation的写/执行错误；已由后续成功mutation恢复的历史错误不再污染
  patch semantic risk，旧日志格式保持保守兼容。
- 忠实翻译InfCodeX FEATURE_178的稳定JSON、20-call ring、三次窗口重复、cache-hit后两次重复、
  signal envelope和16消息第三方视角transcript。L1只负责非阻塞启动L2，触发调用继续执行；只有后续
  `beforeTool`消费一次性nudge并抑制该次dispatch，保持上游one-cycle latency。
- InfCode-dev的连续三次完全相同调用仍是独立的即时权限门；它不等待L2，也不会被L2的`is_stuck=false`
  解除。两套机制按各自源码语义组合，而不是把窗口检测错误替换成同步doom-loop。
- L2支持camelCase/string boolean防御解析；Provider异常、非法结构和5秒超时全部fail-open并写typed
  trace。超时线程和compaction前未完成的verdict通过epoch失效，不能给新上下文注入迟到nudge。
- run开始、自动compaction和手动compaction都重置detector、transcript与pending nudge。
- 默认`AgentHooks`装配strict generation stop consumer：普通产品模式惰性；strict自然结束但证据
  未结算时按InfCodeX两次reanimate budget继续，避免“类存在、主链无consumer”。
- strict调查分类扩展到`cat/grep/head/rg/sed/tail/tree/find/git grep`只读Bash；Git status与
  Python verification不计调查预算。
- strict hard gate第一次拒绝后允许模型转向edit/diff/verify；若下一轮仍请求调查工具，第二次转为
  `strict_terminal_blocker`并终止，避免把剩余轮次全部烧在重复反馈上。
- SWE最终generation之前的显著写入错误不再污染最终patch risk，但以
  `recovered_tool_errors:N`保留为有界process warning；`_runtime_summary`和`run_end` trace现在真实
  输出mutation/diff/verification generation，生产report不再依赖测试手工构造字段。

### 当前证据与边界

所有新增行为均先观察到预期红测，再写生产代码转绿；RuntimeState、Loop、Hook、Composition、
SWE、L1 detector和L2 sidecar共205项核心组合通过，全量为1417 passed；修改模块的
`py_compile`、Ruff和`git diff --check`通过。

provider-free真实Agent在独立临时Git仓库中执行了
`read_file → edit_file → verify_changed_files → diff_status`：4次Provider响应、4个工具均成功，
文件从`value = 1`变为`value = 2`，mutation/diff/verification generation均为1，61条trace以
`run_end=completed`结束且`run_end.runtime`与返回状态一致。因此A226定义的Agent Core生产链达到
`trace_verified`。

这里的“完成”只指A226冻结的生命周期合同，不等于宣称与两个TypeScript仓库逐字节相同，也不等于
已经证明SWE-bench分数相同。付费Provider互操作、真实复杂仓库策略质量和官方Docker harness仍需
下一轮小样本/正式评测给出证据；本阶段没有启动任何付费调用或SWE批跑。

设计与逐步实施合同分别位于
`docs/superpowers/specs/2026-08-10-agent-core-source-parity-design.md`和
`docs/superpowers/plans/2026-08-10-agent-core-source-parity.md`。

## 211F. A227：InfCodeX Main Agent Sidecar Verifier生产闭环

### 211F.1 InfCodeX参考能力

- `packages/agent/src/runtime-middleware/llm-judge.ts`：通用的独立LLM judge调用骨架，负责强制
  report tool、模糊工具名匹配、timeout/cancel和fail-open。
- `packages/coding/src/agent-runtime/middleware/sidecar-verifier/verifier.ts`：三态verdict解析、
  revise retrospective与StopHook映射。
- `verifier-context-builder.ts`和`verifier-prompts.ts`：保留当前真实用户请求、最近24条非system
  transcript、实际修改摘要与Main Agent精确final text，并用第三方视角防止角色混淆。
- `gate.ts`和`verifier-provider-resolver.ts`：按风险/计划/轮次/文件数/修改行数决定是否调用；
  默认继承Main Provider/model，仅在Provider和model两个环境变量同时有效时覆盖。

### 211F.2 NZ-Coder原有不足

A132只有typed StopHook与两次reanimate预算，A226只把SWE strict generation consumer装入默认链。
Workflow的A159 Sidecar又属于Child workflow，不会审核普通Main Agent的自然文本终止。因此此前
“已有Sidecar模块”不等于Main Agent生产闭环：CLI/HTTP/SWE的普通coding runtime不会发起独立
verifier请求，也没有对应gate、Provider解析和三态trace。

### 211F.3 实现结果

- 新增通用`invoke_llm_judge`：Provider异常、timeout、caller cancellation、无report tool和解析失败
  各自保留诊断原因并安全accept；迟到线程结果不能覆盖已结算结果。
- 按上游顺序实现FEATURE_196 gate：escape hatch、risky shell、无路径写入、plan、长run、多文件、
  大修改、trivial observed work和短问候分支均有契约测试。
- Verifier只收到自己的system prompt与一个第三方证据user message，不继承Main Agent system/history；
  请求只暴露并强制`emit_sidecar_verdict`，输出上限1024，默认timeout 15秒。
- `accept`继续原终止链，`revise`注入带provenance的synthetic user guidance并最多返工两次，
  `blocked`以`stopped_by_hook`保留原因；Sidecar排在strict/fallback stop hooks之前。
- CLI、HTTP、本地评测与SWE均经`AgentRuntimeAssembly → AgentLoop`自动获得Sidecar；显式注入client的
  测试/宿主默认保持无隐藏模型调用，也可用`sidecar_verifier`注入确定性替身或用`False`关闭。
- 每次判断记录`sidecar_gate_decision`、`sidecar_started`、`sidecar_finished`，并累计fire/skip、
  verdict和最后trace；显式覆盖创建的独立client由Agent关闭，继承Main client时不重复关闭。

### 211F.4 关键设计决策

没有把TypeScript逐字复制为不能运行的Python，而是保持控制分支、默认值、顺序和失败语义一致，
再适配NZ已有Provider和OpenAI-compatible消息结构。NZ的ChangeTracker能给出真实当前diff，因此文件
证据使用path加changed-line hint，比上游仅有mutation次数更具体；synthetic user消息不计作原始用户
请求，避免两次revise后Verifier把自己的反馈误认为用户需求。

### 211F.5 关键文件

- `nz_coder/runtime/llm_judge.py`：领域无关LLM Judge内核。
- `nz_coder/runtime/sidecar_verifier.py`：prompt/context/gate/verdict/provider/hook与统计。
- `nz_coder/runtime/hooks.py`：同步/异步StopHook共用同一decision landing。
- `nz_coder/runtime/loop.py`：生产默认装配、运行指标、异步自然停止和资源关闭。
- `tests/test_llm_judge.py`、`tests/test_sidecar_verifier.py`、`tests/test_hooks.py`、
  `tests/test_loop_fake.py`：源码语义与真实离线Agent链路合同。

### 211F.6 验证结果

- 静态检查：修改模块`compileall`和`git diff --check`通过。
- 定向测试：Sidecar/LLM Judge/StopHook相关28项通过。
- 完整测试：`1440 passed`，仅保留Python 3.13 fork多线程的既有DeprecationWarning。
- 真实冒烟：真实`AgentLoop`使用离线Provider先产生Main final，再收到隔离Verifier请求并accept；
  trace确认gate/start/finish全链存在，且Verifier请求只有system/user与单一强制report tool。
- 是否运行评测：没有恢复Lite/Verified，没有调用DeepSeek或其他付费Provider。

### 211F.7 学习重点

1. StopHook类型存在不代表验证能力存在；必须有生产装配、独立请求、verdict landing和资源owner。
2. Sidecar的关键不是“再问一次模型”，而是角色隔离、客观修改证据、有限返工和失败时不破坏主链。
3. 自动Verifier会增加请求成本，因此上游gate的分支顺序与trivial skip同Provider调用本身一样重要。
4. cancellation必须越过async wrapper抵达后台judge；否则Agent已取消，Verifier仍会占用Provider连接。

### 211F.8 剩余差距

- 本阶段已达到本地源码语义和production trace闭环，但没有做真实DeepSeek/OpenAI/Anthropic/Gemini
  互操作；不同服务对forced tool choice的兼容性仍需凭据允许后的脱敏live smoke证明。
- `additionalCriteria`当前来自NZ RuntimeState验收标准；InfCodeX FEATURE_247完整profile verification
  contract仍需在NZ出现同构profile consumer时再接，不应凭空增加第二套任务协议。
- 本阶段不证明整个Agent Core与InfCodeX所有未来commit相同，也不证明SWE-bench分数相同。

设计与实施记录位于
`docs/superpowers/specs/2026-08-10-agent-core-sidecar-verifier-source-parity-design.md`和
`docs/superpowers/plans/2026-08-10-agent-core-sidecar-verifier-source-parity.md`。

## 211G. A228：统一Agent Runtime架构Phase 0–1

### 211G.1 InfCodeX参考能力

- `packages/agent/src/primitives/runner.ts`：Runner负责统一的Agent生命周期和terminal invariant。
- `packages/agent/src/primitives/agent.ts`：Agent是声明数据，而不是同时持有Provider、Session和UI的对象。
- `packages/coding/src/agent.ts`、`coding-preset.ts`与`agent-runtime/run-substrate.ts`：稳定SDK入口经
  Runner frame进入Coding substrate。
- `packages/coding/src/child-executor.ts`：read/write child负责scope、worktree、profile和结果包装，最终
  重新进入`runKodaX(agentMode='sa')`，而不是复制第二套Provider/tool loop。
- `agent-runtime/context-budget.ts`、`tool-dispatch.ts`和`tools/registry.ts`：预算、调度和注册具备可独立
  测试的边界。

### 211G.2 NZ-Coder原有不足

`AgentLoop`类约6857行，构造并编排Provider、MCP、Session、Context、Tool、Transaction、Verification、
Memory、Skills、Background Agent、Trace和UI question。`run_subagent()`又包含约1188行独立Provider/
工具/重试/事务/验证/终止循环。主/子Agent因此只能靠重复修补保持近似行为，不能由架构保证一致。

依赖分析还确认runtime/state/tools/providers/session/workflow存在双向或环形依赖，尤其Core Runtime反向
import终端问题组件、Session清理反向import AgentManager。这些边界使HTTP、CLI、SWE和child难以共享一个
可嵌入执行内核。

### 211G.3 实现结果

- 冻结兼容迁移方案：保留`AgentLoop(...)`、`agent.run(...)`、`run_subagent(...)`，逐阶段让它们委托
  新Runner，不做一次性重写。
- 新增`RunMode`和五种不可变`RunProfile`，显式描述main/read-child/write-child/background/workflow的
  mutation、child spawn、interactive question和session能力。
- 新增不可变`AgentDefinition`、`RunRequest`、`RunResult`、`TokenUsage`；RunRequest对messages、tools、
  metadata和workspace做边界快照/规范化。
- 新增唯一可变`RunState`，负责每个Runner frame的transcript、turn、usage、terminal和parent关联；
  terminal后拒绝继续追加消息或启动turn。
- 新增runtime-checkable `ModelGateway`、`ToolRuntime`、`ContextManager`、`SessionRepository`、
  `MemoryService`、`CompletionVerifier`、`RuntimeEventSink` Protocol以及`RuntimeServices`组合验证。
- 用characterization test确认真实legacy接口：`AgentLoop.run()`原地更新传入messages，返回字典没有
  `messages`键；child同步/异步入口的十个公开参数被冻结，后续facade迁移不得误改。

### 211G.4 关键设计决策

没有直接让child调用当前巨大`AgentLoop`，因为那只会把God Object升级为最终内核；也没有一次性写
Runtime V2，因为现有恢复、评测和Session行为太多，容易静默丢失。选择strangler migration：先建立
contract和行为基线，再依次抽Provider、Tool、Context/Session，最后建立共享Runner并迁移child。

也没有机械复制InfCodeX的generic Runner与coding substrate双路径。InfCodeX本身仍有历史双路径；NZ的
目标是借鉴Agent-as-data、Runner生命周期和child复用语义，最终只保留一个Python执行内核。

### 211G.5 关键文件

- `docs/superpowers/specs/2026-08-10-unified-agent-runtime-architecture-design.md`：完整目标架构和八阶段路线。
- `docs/superpowers/plans/2026-08-10-unified-agent-runtime-phase-0-1.md`：当前TDD实施合同。
- `nz_coder/runtime/core/profiles.py`：运行模式和能力profile。
- `nz_coder/runtime/core/request.py`：Agent声明和不可变Run输入。
- `nz_coder/runtime/core/result.py`：统一终态与token使用量。
- `nz_coder/runtime/core/state.py`：单frame可变状态owner。
- `nz_coder/runtime/core/contracts.py`、`events.py`：依赖反转端口与事件envelope。
- `tests/runtime/core/`：新contracts和legacy facade基线。

### 211G.6 验证结果

- 静态检查：`compileall`和Ruff通过；`runtime.core`可独立import且不依赖interface、具体Provider、tools、
  sessions、AgentLoop、subagent或AgentManager。
- 定向测试：新增runtime/core 30项通过；与composition/child/subagent合并的76项聚焦通过。
- 完整测试：`1470 passed`；仅有Python 3.13已有multiprocessing fork DeprecationWarning。
- 真实冒烟：本阶段没有切换生产执行链，因此不伪造Runner真实冒烟；legacy Main facade通过真实
  `AgentLoop + FakeClient`完成一次non-streaming turn。
- 是否运行评测：没有调用付费Provider，没有运行Lite/Verified或官方harness。

### 211G.7 学习重点

1. 文件和模块齐全不等于Runtime统一；必须检查实际入口最终进入哪一个循环。
2. contract必须先冻结所有权和生命周期，不能只是为旧God Object增加更多helper。
3. characterization test应记录真实API而非愿望；本阶段由红测纠正了“run结果含messages”的错误假设。
4. child安全来自profile与隔离服务配置，Provider/tool/context/terminal语义则必须来自同一Runner。

### 211G.8 剩余差距

- 当前只达到`contract_verified`，没有生产consumer，不能称为统一Runner已经wired。
- Phase 2需要把主/子两条Provider选择、请求、超时、retry、overflow和usage路径统一为`ModelGateway`。
- Phase 3–4仍需抽取完整Tool pipeline、Context/Compaction和Session repository。
- Phase 5–6完成共享`AgentRunner`与child迁移、provider-free生产trace后，才可把执行内核标为
  `trace_verified`；在此之前不得声称Agent Core已完全对齐InfCodeX。

设计与实施计划分别位于
`docs/superpowers/specs/2026-08-10-unified-agent-runtime-architecture-design.md`和
`docs/superpowers/plans/2026-08-10-unified-agent-runtime-phase-0-1.md`。

## 211H. A229：统一Agent Runtime Phase 2 ModelGateway生产边界

### 211H.1 InfCode/InfCodeX参考能力

- InfCode的Provider/Session调用链把模型能力、请求转换、流式事件、usage和错误恢复集中在稳定边界，
  Session消费者不应各自解释SDK响应。
- InfCodeX的`Runner → coding substrate → child executor → runKodaX`强调Main和child复用同一种模型调用、
  生命周期和终态语义；child只增加scope/worktree/profile，而不复制Provider loop。
- 两者共同说明：文件名相似或增加一个转发wrapper不算源码级对齐，必须让生产调用者实际经过同一
  timeout、cancel、retry、overflow和usage owner。

### 211H.2 NZ-Coder原有不足

Phase 0–1虽然建立了`ModelGateway` Protocol，但生产链尚未wired。`AgentLoop`仍自行解析流/非流响应、
重试和计费；`run_subagent()`另有线程超时、Retry-After、usage/cost循环；compaction、memory、sidecar、
vision又分别直接调用Provider或OpenAI client。相同429、400、取消或上下文溢出会因调用者不同而产生
不同结果，Main/child parity只能靠重复修补。

### 211H.3 实现结果

- `ModelSelectionRequest → ResolvedModelRuntime`现在一次性解析Provider、逻辑模型、wire模型、variant、
  capability、registry pricing和client所有权；注入client永不误关，内部client幂等关闭。
- `ModelCall`、`ModelCallOutcome`、`ModelStreamEvent`和`NormalizedUsage`形成不可变协议；所有调用都有
  明确purpose、输出上限、超时、工具、response format、终态、attempt、usage、cost和错误身份。
- buffered Gateway统一硬超时、调用前/调用中取消、400/422、认证、上下文溢出、429/5xx/连接错误、
  Retry-After/指数退避、JSON response-format单次降级和迟到worker隔离。
- streaming Gateway统一idle/hard timeout、think-tag/native reasoning、text/tool/provider metadata/usage事件、
  partial turn清理后重试、稳定边界前单次buffered fallback，并保持tool-call finish后先执行工具、再消费
  trailing usage；写工具已执行后的流错误不会重放副作用。
- Main non-stream/stream、planning、replanning、stall sidecar、workflow文本协议、child普通turn与structured
  repair、compaction、memory extraction/rerank、Sidecar Verifier和vision全部通过Gateway。
- AST架构测试扫描`nz_coder/runtime`、`state`和`vision.py`，禁止在`runtime/model_gateway`之外出现
  `create_completion`、`chat.completions.create`或`responses.create`调用。

核心生产调用链为：

```text
Agent/child/auxiliary consumer
  -> immutable ModelCall
  -> ProductionModelGateway
  -> ResolvedModelRuntime
  -> Provider adapter/client
  -> ModelCallOutcome + ModelStreamEvent
  -> Session/tool/memory consumer projection
```

### 211H.4 关键设计决策

没有把SessionProcessor或工具执行搬进Gateway。Gateway只拥有模型调用政策，Main仍把stream event投影到
durable Session，并在tool-call稳定边界调用工具；否则新Gateway会变成另一个God Object。Provider adapter
仍是协议/wire格式owner，Gateway不重复实现Anthropic、Gemini、Responses或OpenAI序列化。

保留`AgentLoop._call_streaming/_call_non_streaming`和`_completion_with_timeout`兼容入口，但前两者只投影
Gateway结果，后者只委托`runtime/model_gateway/compat.py`。这既保持外部接口和历史故障注入测试，又由
架构测试保证Agent Core不能恢复第二条SDK路径。

### 211H.5 关键文件

- `nz_coder/runtime/model_gateway/models.py`：不可变调用、事件和终态。
- `nz_coder/runtime/model_gateway/runtime.py`：Provider/model/client解析与资源所有权。
- `nz_coder/runtime/model_gateway/gateway.py`：buffered/stream调用政策、归一化与兼容client bridge。
- `nz_coder/runtime/model_gateway/stream.py`：阻塞stream的idle/hard timeout和安全关闭。
- `nz_coder/runtime/model_gateway/usage.py`、`errors.py`：token/cost与错误分类唯一实现。
- `nz_coder/runtime/loop.py`、`subagent.py`：Main/child生产consumer与legacy结果投影。
- `nz_coder/state/context.py`、`memory.py`、`runtime/sidecar_verifier.py`、`vision.py`：辅助调用迁移。
- `tests/runtime/model_gateway/`：行为、资源、stream、usage和AST边界合同。

### 211H.6 验证结果

- 静态检查：目标模块`compileall`通过；Ruff `All checks passed`；AST边界检查确认Agent Core零绕过。
- 定向测试：Gateway、Session events、structured output、Main loop和child等高风险组合`179 passed`。
- 完整测试：`1504 passed, 1 warning`；warning为既有Python 3.13多线程进程fork提示。
- 真实冒烟：使用真实生产`AgentLoop.run()`与`run_subagent()`、Fake Provider/client完成buffered、stream、
  tool delta、tool execution、retry、fallback、usage和资源关闭；没有伪造第二套Runner入口。
- 是否运行评测：没有调用付费Provider，没有恢复Lite/Verified或官方harness。

### 211H.7 学习重点

1. 统一Gateway的判断标准是生产consumer零绕过，不是存在一个名为Gateway的类。
2. model metadata和client必须由同一个runtime快照拥有，否则切模型、child和sidecar仍会串用配置。
3. stream的稳定边界不是“收到任意字符”：可撤销partial Session part可以重试，已执行工具则绝不能重放。
4. usage必须先拆分cache/reasoning互斥桶，再按Provider账单优先、registry次之计算，不能由各consumer重复算。
5. cancellation不能只取消await；后台线程的迟到结果必须失去发布权，资源owner还必须覆盖所有return路径。

### 211H.8 剩余差距

- Phase 2只统一了模型边界，`AgentLoop`与`run_subagent()`尚未委托同一个完整`AgentRunner`；Tool pipeline、
  Context/Session repository和terminal lifecycle仍需按Phase 3–6继续抽取。
- `runtime/model_gateway/compat.py`只为冻结的legacy raw-response入口保留；新生产代码不得新增消费者，最终
  shared Runner稳定后可以删除该兼容层。
- 本地Fake Provider覆盖不能替代OpenAI/Anthropic/Gemini/DeepSeek真实endpoint互操作，也不能证明
  SWE-bench分数与InfCodeX相同；这些需要用户允许成本后单独验证。

设计与实施记录位于
`docs/superpowers/specs/2026-08-10-unified-model-gateway-design.md`和
`docs/superpowers/plans/2026-08-10-unified-model-gateway.md`。

## 211I. A230：统一Tool/Context/Session边界与共享AgentRunner基础

### 211I.1 实现结果

- `runtime/tool_runtime/scheduler.py`成为并发读段、串行副作用屏障、队列等待统计和协作取消的唯一实现；`loop.py`只重导出旧私有测试入口。
- `ProductionToolRuntime`持有批事务、guardrail前后处理、dispatch、结果投影、handoff、写后索引/LSP/风险刷新以及取消回滚的固定顺序；`AgentLoop._execute_tools*`与`_dispatch_tool_calls*`已降为兼容facade。
- `ProductionContextManager`持有输入展开、超长User落盘、旧Tool结果剪枝、Provider usage溢出判断和语义压缩触发；`FileSessionRepository`按RunRequest workspace/session隔离并保留既有JSON格式。
- 新增`AgentRunner`，其RunState独占transcript/turn/usage/terminal状态，通过RuntimeServices执行load→context→model→tool→save→verify→finalize，并发出统一RuntimeEvent。

### 211I.2 关键边界与验证

- Tool/Context/Session/Runner模块均不导入`AgentLoop`；AST测试阻止生命周期实现重新回流God Object。
- 真实取消、PDF worker settlement、事务回滚、图像Read、并发顺序和legacy facade共109项组合通过；Runner/Session/core契约34项通过；最终全量`1513 passed, 1 warning`，Ruff通过。
- 本阶段没有运行付费Provider或SWE-bench。warning仍是Python 3.13多线程进程fork提示。

### 211I.3 剩余差距

- A230不是完整源码级终态：`AgentRunner`状态机已经存在并通过契约测试，但生产`AgentLoop.run()`尚未整体委托它，child的隔离准备之后仍保留独立for-turn循环。
- 下一步必须完成Main facade切换、child profile切换、background/workflow切换，然后删除旧循环和反向依赖；在这些完成前不能宣称Agent Core与InfCodeX完全同核。

## 211J. A231：统一Agent Runtime生产切换、依赖收口与公共SDK

### 211J.1 InfCodeX参考能力

- `packages/agent/src/primitives/runner.ts`提供公共Runner frame与coding preset dispatcher。
- `packages/coding/src/agent-runtime/run-substrate.ts`提供coding substrate；
  `packages/coding/src/child-executor.ts`只拥有child隔离、配置和结果包装，最终调用同一`runKodaX`。
- 核心原则是一套执行语义、多种Agent配置，而不是Main与child各维护一套Provider/tool循环。

### 211J.2 NZ-Coder原有不足

- A230虽建立了`AgentRunner`，但当时简化SDK循环和生产`run_host`循环同时存在，属于同类职责的两套状态机。
- `run_subagent()`仍没有直接进入Runner，`FileSessionRepository`也尚无生产checkpoint consumer。
- `state.sessions`直接导入AgentManager/Worktree，runtime又导入terminal timeline/question adapter，依赖方向没有闭合。
- `AgentLoop.run()`仍直接管理MCP启动、ContextVar栈、memory/skills/tool binding，资源生命周期和turn orchestration混在一个入口。

### 211J.3 实现结果

- `AgentRunner.run()`成为Main、child、background、workflow和SDK的统一入口；唯一turn循环为
  `_run_turns()`，旧简化RuntimeServices循环已删除。
- `ProductionRuntimeHost`接管workspace、Session、MCP、Memory、Skill、tool state、后台manager和交互回调的绑定、取消与错误结算。
- `AgentLoop.run()`和`_run()`均为兼容facade；`run_subagent()`在隔离/路由准备后直接调用
  `agent.runner.run(agent, messages)`，不包含`for turn_index`、`ModelCall`或Gateway构造。
- AgentManager与Workflow保留slots/cancel/worktree/aggregation职责，Agent执行委托`run_subagent()`；持久状态将
  background/workflow profile与read/write child execution surface显式区分。
- `FileSessionRepository.checkpoint()`接入生产step边界，`AgentLoop`不再直接`save_session()`。
- Session清理改成state声明callback port、runtime安装AgentManager/Worktree adapter；fork transcript重键从terminal timeline下沉到`message_schema`；AST守卫禁止整个runtime import interface。
- `nz_coder.sdk.AgentClient`/`run_agent()`按`RunRequest`构造生产Agent，沿CLI/HTTP相同链执行并投影稳定`RunResult`，不再维护测试专用执行循环。

### 211J.4 关键设计决策

- 没有机械复制TypeScript的Promise/AbortController/worker结构；Python侧使用asyncio、ContextVar、context manager、Protocol和dataclass。
- Runner负责顺序，RuntimeHost负责资源作用域，Tool/Context/Session服务负责完整能力；这种分法既避免第二内核，也避免把所有setup塞进Runner。
- 保留`AgentLoop`旧构造与结果字典，避免破坏CLI/HTTP/SWE；SDK在边界投影`RunResult`。
- 保留实例级`_run`覆盖和`loop.MCPRuntime`注入兼容点，但它们只适配旧embedding/测试，不构成生产第二循环。

### 211J.5 关键文件

- `nz_coder/runtime/runner.py`：唯一生产Agent状态机与`agent_runner_enter`证据。
- `nz_coder/runtime/host.py`：run-scoped资源生命周期。
- `nz_coder/runtime/subagent.py`：child隔离/profile/result facade。
- `nz_coder/runtime/tool_runtime/`：工具调度和批生命周期。
- `nz_coder/runtime/context_manager.py`：模型窗口预检和压缩触发。
- `nz_coder/runtime/session_repository.py`：生产checkpoint和RunRequest持久化适配。
- `nz_coder/runtime/session_cleanup.py`、`nz_coder/state/sessions.py`：依赖倒置的Session清理端口。
- `nz_coder/sdk.py`：公共Python嵌入接口。
- `docs/unified-agent-runtime-migration.md`：完整InfCodeX→NZ-Coder架构映射、目标结构与迁移报告。

### 211J.6 验证结果

- 静态检查：`python -m compileall -q nz_coder tests`与`python -m ruff check nz_coder tests`通过。
- 定向测试：Runner/SDK/Tool/Context/Session/Main/child/AgentManager/Workflow/依赖AST组合通过；新增测试均经历红→绿。
- 完整测试：`1525 passed in 91.57s`。
- 真实冒烟：真实`nz-coder` PTY进入全屏终端并输入`exit`正常恢复终端、输出`Goodbye!`；真实
  `nz-coder serve`返回`{"status":"ok","service":"nz-coder"}`并可由Ctrl+C正常关闭；Main生产trace顺序为
  `agent_runner_enter → run_start → llm_response → run_end`，child独立trace也包含`agent_runner_enter`。
- 是否运行评测：没有调用付费Provider，没有恢复SWE-bench；本轮只证明架构/执行链，不声称分数与InfCodeX相同。

### 211J.7 学习重点

1. 同一个类里放两个循环不叫统一；必须让所有生产入口进入同一个状态机并有trace证据。
2. child的差异应是profile、workspace、tool scope和结果包装，不应是另一套Provider/tool loop。
3. 抽出一个repository文件但生产仍直接写盘，只是`mechanism_only`；consumer接线后才是`wired`。
4. 依赖倒置不是把import改成动态import，而是让低层声明port、上层安装adapter。
5. 大文件不是唯一判断标准；关键是它是否仍拥有重复生命周期和控制循环。

### 211J.8 剩余差距

- `runtime/loop.py`仍是较大的coding capability adapter，包含prompt materialization、reflection、verification、handoff等一份实现；可继续按能力拆分，但已没有Main/child重复执行内核。
- 公网Provider/MCP互操作、SWE-bench Verified分数、daemon/IDE生态仍需单独外部证据，不能从离线架构测试外推。

## 212. 下一步对齐候选

以下内容只表示候选方向，不代表已经完成。选择时按“上游影响范围优先、已有主链路补短板优先、没有 consumer 的产品面后置”判断：

| 候选 | 进入条件 | 最小完成标准 |
|---|---|---|
| SWE子进程假超时与patch保全 | 立即P0；当前r3已确认22个已完成任务被误判 | >64KB日志不会阻塞退出；completed/error/timeout唯一终态；trace独立落盘；已生成patch不会因IPC错误静默清空。 |
| Strict阶段收敛与终端验证 | 修复IPC后立即处理 | 参数变化的无进展调查可被有界识别；strict grammar模型可见；成功验证通过runtime terminal signal收口；普通产品模式不受SWE策略污染。 |
| Instruction change event / terminal consumer | 用户恢复CLI产品对齐，或GUI/IDE需要实时刷新instruction设置时 | 文件watcher有debounce/lifecycle owner并进入现有Session event协议；终端入口必须有list/toggle反馈与删除确认，不能只注册空命令。 |
| Repo-intel cancellation/reliability | NZ本地smart_search、references或map扫描在大仓出现真实长阻塞时 | 以NZ本地producer为可靠性增强，接入event/进程终止/有界输出；除非上游存在同构链路，不称为DeepMap parity。 |
| PDF/audio/video FilePart | 至少一个目标 Provider 和真实产品入口明确支持时 | producer、MIME/大小边界、Session owner、compaction 与 Provider wire consumer 全部闭环；不以 schema 能保存代替可用。 |
| Network/offline终端状态 | 真实运行仍因断网阶段显示盲等时 | 只消费既有Session Part/Event；若缺producer先补有owner的状态链，终态唯一、取消可结算，不增加第二事实源。 |
| 跨平台 wheel CI | 有 macOS/Windows runner 或准备公开发布时 | Python 3.9–3.13 至少覆盖 Linux，macOS/Windows 各一版本；执行 install/help/doctor 和 terminal input smoke，不能用单机推断。 |
| SWE-bench 可复现证据 | 用户启动正式打榜时 | 固定commit/model/config完成Verified 500严格pass@1，validator通过并保留公开轨迹/官方提交包；Lite只做冒烟，不得混入retry或答案联网。 |
| 公网 Provider/MCP 互操作 | 用户提供或批准测试 endpoint、凭据与网络动作时 | 记录脱敏请求边界、版本、成功/失败矩阵；本地 fixture 不能替代该证据。 |
| 第三方插件 runtime | 出现至少一个真实外部插件 consumer 时 | 基于 A035 增加显式 trust、版本/compatibility、隔离加载和 unload；不能先做 marketplace 空壳。 |
| SDK/IDE host | 用户明确将 NZ-Coder 产品目标升级为 App/IDE 时 | 以统一 Session API 为唯一后端，先交付一个真实 consumer，不再创建平行控制面。 |

HTTP transport 已在 A023 完成当前传输正确性闭环，A091 又补齐显式Session物理删除；这不等同于扩建HTTP产品面。自动归档/retention、远程认证、GUI/IDE host与资源配额仍只有在出现明确consumer或用户重新指定产品目标时恢复。

选择下一项前，应先重新阅读当前 InfCode 源码，不能只根据旧文档或印象实现。

## 213. 每次更新本文档的模板

完成新的对齐项后：

1. 在“对齐进度总览”追加一行；
2. 在文档末尾、更新记录之前新增详细章节；
3. 使用以下结构，不能省略验证和剩余差距。

```markdown
## N. Axxx：能力名称

### N.1 InfCode 参考能力
- 参考文件：
- 核心行为：

### N.2 NZ-Coder 原有不足
- 缺失能力：
- 对 Agent 的实际影响：

### N.3 实现结果
- 核心调用链：
- 新增或修改的行为：

### N.4 关键设计决策
- 为什么这样实现：
- 为什么没有直接复制 InfCode：

### N.5 关键文件
- `path`：职责

### N.6 验证结果
- 静态检查：
- 定向测试：
- 完整测试：
- 真实冒烟：
- 是否运行评测：

### N.7 学习重点
1. ...

### N.8 剩余差距
- ...
```

## 214. 文档更新记录

| 日期 | 更新内容 |
|---|---|
| 2026-08-10 | 完成A231统一Agent Runtime Phase 5–8：Main/child/background/workflow/SDK进入唯一AgentRunner，新增ProductionRuntimeHost，删除child与简化SDK重复循环，Tool/Context/Session完成生产接线，消除state→runtime与runtime→interface反向依赖；1525项全量、compileall、Ruff、Main/child trace、真实PTY和HTTP health通过，未跑付费Provider/SWE。 |
| 2026-08-10 | 完成A230的Phase 3–5架构基础：ProductionToolRuntime接管调度/取消/事务/批生命周期，ProductionContextManager接管预检与压缩触发，FileSessionRepository保留既有格式，AgentRunner通过Fake services完成模型→工具→模型闭环；AgentLoop入口降为兼容facade。1513项全量与Ruff通过；Main/child/background最终切换仍未完成，未跑付费Provider/SWE。 |
| 2026-08-10 | 完成A229统一Agent Runtime Phase 2到`production_verified`：新增ResolvedModelRuntime和ProductionModelGateway，统一Main/child/规划/压缩/记忆/verifier/stall/vision的buffered/stream请求、timeout/cancel/retry/fallback/overflow、usage/cost、错误身份和client生命周期；AST守卫确认Agent Core零绕过。179项高风险组合、1504项全量、compileall/Ruff通过；未跑付费Provider/SWE，完整共享AgentRunner仍属Phase 3–6。 |
| 2026-08-10 | 完成A228统一Agent Runtime Phase 0–1到`contract_verified`：源码确认Main `AgentLoop`与child `run_subagent`是重复执行内核，批准兼容facade迁移；新增Agent/profile/request/result/RunState、事件和七类服务Protocol，并冻结legacy Main返回形状与child签名。30项新增、76项聚焦、1470项全量及静态/依赖隔离检查通过；尚未wired新Runner，未跑付费Provider/SWE。 |
| 2026-08-10 | 完成A227到`trace_verified`：源码转译InfCodeX通用LLM Judge与Main Agent Sidecar Verifier，补齐24消息第三方上下文、真实文件证据、FEATURE_196 gate、强制单一verdict工具、三态landing、15秒/取消/fail-open、Provider继承/覆盖、sidecar-first生产装配和trace统计。28项聚焦、1440项全量、静态检查与真实离线双请求Agent链通过；未跑付费Provider/SWE。 |
| 2026-08-10 | 完成A226到`trace_verified`：正确组合InfCode即时连续三连权限门与InfCodeX 20-call L1/异步L2 one-cycle nudge；补齐16消息transcript、5秒fail-open、epoch/compaction reset、strict二次final blocker、恢复期warning及run_end generation证据。205项核心组合、1417项全量、静态检查通过；provider-free真实Agent完成read→edit→verify→diff，未跑付费Provider/SWE。 |
| 2026-08-10 | 完成A225诊断：不重跑旧题，以A224新源码指纹建立20题续片并新增`--max-new-instances`安全暂停；20条持久结果中18个Agent实例全部产出patch且trace正常结束，2个Git/TLS setup失败；确认只读Bash绕过调查门、hard gate重复、验证顺序、历史risk归因、strict grammar/命令可用性与结构化工具采用率六项问题。相关80项回归通过，未跑官方harness。 |
| 2026-08-10 | 完成A224：修复SWE子进程Queue大载荷假超时，新增strict Bash workdir/模型可见语法、12/20 mutation-generation调查收敛、source-only验证terminal signal及过程policy/patch risk分离；227项聚焦、1394项完整回归、py_compile与Ruff通过，未运行新评测。 |
| 2026-08-10 | 完成A223源码复核（未实现）：以112个r3 raw trace反查InfCodeX eval runner/Runner/stall sidecar/terminal signal与infcode-dev Session finish/doom-loop/Bash permission链；确认22/28 agent_failed为Queue join-before-drain假超时，Main Agent stop-hook只有机制未装配，精确三连不能覆盖参数变化的语义游走，并冻结五步P0/P1修复顺序。 |
| 2026-08-09 | 完成A222严格打榜闭环：Verified 500成为唯一主榜，Lite 300降为冒烟；pass@1提示/工具/恢复边界禁止hints、官方测试知识和答案联网，公开轨迹与官方提交包fail-closed；默认模型切换deepseek-v4-flash。22项严格契约、203项聚焦与1357项完整回归通过，尚未运行500题。 |
| 2026-08-09 | 完成A221真实证据闭环：修复失效SWE matplotlib `.pth`环境污染并固定Ruff开发版本；DeepSeek text/tool/stream与真实PTY read_file通过，Python3.13全量1335 passed；固定顺序前10个SWE-bench Lite first-pass官方结果6 resolved/4 unresolved/0 errors，记录四类失败根因，未将小样本外推为300题成绩。 |
| 2026-08-09 | 完成A214–A220最终闭环：Provider Attempt Controller/stream watchdog/单次buffered fallback，Provider-backed Workflow生成与两次repair共享deadline，终端digest审批和`/workflow`命令，异步first-started SDK、跨进程run identity关闭式恢复，Provider/MCP显式live smoke入口及SWE source/config/instance manifest；1333项完整回归、Ruff、CLI dry-run和真实PTY通过，机器contract升级到1.6；公网与300实例评测未运行。 |
| 2026-08-09 | 完成A194–A213连续二十阶段：approval digest/stale gate/typed outcome/headless receipt，run/saved rename/recoverable delete/revision replace/result/retention/history，JSON generation extraction/envelope/timeout/repair/tool，以及Main/Child tool-name repair、tool result分类、retry/terminal diagnostics；28项新增专项、239项核心组合、1324项完整回归及Ruff通过，机器contract升级到1.5。 |
| 2026-08-09 | 完成A184–A193连续十阶段：安全historical identity、display alias与歧义关闭、command-only invocation、turn-consumption、min-wins host limits、approval summary、真实runtime ceilings、display持久链、identity-aware resume和scout-then-author Host API；14项新增专项、95项核心组合、1296项完整回归及Ruff通过，机器contract升级到1.4。 |
| 2026-08-09 | 完成A174–A183连续十阶段：builtin-first解析、一级嵌套及共享资源、trusted registry、parallel investigation、immutable review packet、scoped review、quality gate、worktree sweep和六模式JSON-only generator；17项新增专项、81项Workflow/Manager核心组合、1282项完整回归及Ruff通过，机器contract升级到1.3，未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A169–A173连续五阶段：JSON-only Capsule、requirements preflight、project/personal保存发现、saved Capsule真实执行与provenance、run/artifact读取及recoverable archive；6项新增专项、149项核心组合、1264项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A164–A168连续五阶段：严格manifest/admission、managed run pause/resume/stop、0600 bounded artifact、journal-native workflow log及per-run terminal/cost record；修复active stop误发布completed竞态，7项新增行为、142项核心组合、1258项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A159–A163连续五阶段：fresh sidecar三态验证与返工、token budget/abort、可选spawn-process硬终止、workflow outcome幂等有界Lineage，以及versioned机器parity contract；24项新增聚焦、85项核心组合、1251项完整回归与Ruff通过，未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A154–A158连续五阶段：gated synthesis、phase/pipeline/map-reduce、spawn前quality preflight、content-addressed successful-result resume，以及fsynced workflow journal→SessionEvent/HTTP SSE桥接；9项新增Workflow专项、150项核心组合、1236项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A153：按InfCodeX workflow runtime/runPool源码分离maxAgents生命周期额度与maxConcurrency活跃额度，原子化fan-out admission，保持普通失败隔离与结果顺序，并修正terminal发布晚于slot释放造成的虚假并发峰值；26项专项、1227项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A152：按InfCodeX doWait/stopActiveTask/terminalTaskIds源码统一batch wait共享总deadline、timeout cooperative stop、有界settle、幂等stop与唯一terminal event；AgentLoop关闭先结算未等待child再关闭Session资源。24项专项、1225项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A150/A151：按InfCodeX WorkflowProcessTracker/EventRecorder源码新增background revisioned snapshot、counts/progress/token/summary统一投影，以及0600 append-only lifecycle journal、sequence+parent链验证、snapshot重放、截断尾恢复和events cursor consumer；128项聚焦、1220项完整回归与Ruff通过。 |
| 2026-08-09 | 连续完成A145–A149：按InfCodeX child-executor/workflow adapter源码补齐routeFacts与fast-write保护、四类evidenceRefs安全briefing、机器postcondition、一次同Session verification repair，以及full finalText与presentation excerpt分离；修复structured repair重复Assistant持久化。114项聚焦、1215项完整回归与Ruff通过；未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A144：按InfCodeX WorkflowTaskResult/KodaX child result语义新增canonical `ChildAgentResult`，统一前台task、background status/apply和as-tool caller；旧task接入output_schema、固化resume合同及一次持久化无工具repair，legacy `child_*`保持兼容投影。101项聚焦、排除既有环境污染用例后的1202项完整回归与Ruff通过；未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A143：按InfCodeX workflow structured-output源码为terminal Agent增加output_schema、声明期Schema子集审计、fenced JSON提取、递归验证和一次transcript-seeded无工具repair；validated value统一进入result/Assistant/as-tool caller，未验证candidate不发布。42项聚焦、1195项完整回归与Ruff通过；未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A142：按InfCodeX admission/Runner源码实现不可信Agent system cap、准入裁剪、typed handle、具体Bash运行时二次约束、轮次min-wins和run-scoped invariant session；commit后记录mutation，完成时执行finalOwner/evidenceTrail并持久违规。33项聚焦、1186项完整回归与Ruff通过；未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A141：新增唯一AgentRuntimeAssembly，默认coding与显式declared graph互斥；CLI/HTTP/local eval/Aider/SWE入口统一构造，profile/control-plane进入trace/lineage/result；terminal summary经过output guardrail并进入最终可见输出。同步更新架构文档，107项入口组合、1173项完整回归与Ruff通过。 |
| 2026-08-09 | 完成A138–A140：`as-tool` Caller frame原子持久化与崩溃恢复；每Agent provider/model/effort、模型能力预算和reasoning default/max返工升级；input/output/tool before/after四点guardrail及四类verdict进入真实执行路径。186项核心组合、Ruff和排除既有环境污染用例后的1170项完整回归通过；未运行SWE-bench/付费Provider。 |
| 2026-08-09 | 完成A137：补齐InfCodeX `as-tool` handoff，使用隔离目标transcript、Caller frame、自然结束/terminal返回、untrusted agent-result、switch event和lineage；180项聚焦、排除既有环境污染用例后的1164项全量与Ruff通过，并明确run-local stack尚不能跨崩溃恢复。 |
| 2026-08-09 | 完成A134–A136：实现声明式Agent/Handoff DAG、execution-local emit工具、事务后切换、inputFilter、HandoffPart、terminal signal、角色工具schema+dispatch guardrail；新增0600 append-only lineage、断链/截断恢复、active Agent续跑、artifact ledger、幂等memory outcome及压缩后有界recovery seed；169项聚焦、排除已确认环境污染用例后的1163项全量与Ruff通过。 |
| 2026-08-09 | 完成A131–A133：按官方InfCodeX Runner/session-lineage源码补齐child sibling/worker有界消息、seen_by循环防护、settled-boundary消费、typed stop-hook与2次reanimate预算，并把child parent/session/agent/trace/files/conflicts/verification作为结构化outcome进入RunEvidence；同时修复Python `.pth`启动警告导致成功验证被误判失败；241项聚焦与Ruff通过，未运行SWE-bench。 |
| 2026-08-09 | 完成A130并做真实产品复核：DeepSeek read step运行中提交follow-up，trace证明旧run仅1次LLM请求并以interrupted结算，新run返回指定文本；同时跳过相同snapshot的空Session patch重建，接管等待从约8.03秒降至580.7ms；双版本1144项与双wheel PTY通过。 |
| 2026-08-08 | 完成A128/A129：详情增加独立虚拟滚动，mouse drag不误触且ToolPart有hover；五个InfCode message action进入workspace `/keybind`配置并在同一Application热更新；双版本1141项及安装wheel keybind PTY通过。 |
| 2026-08-08 | 完成A126/A127：以structured transcript保留Message/Part身份和渲染锚点，补齐first/last/next/previous/last-user、鼠标消息详情及hidden/compact/full ToolPart原位展开；双版本1137项及wheel PTY通过。 |
| 2026-08-08 | 完成A125：修复长期full-screen漏迁Ctrl+X leader map，新增Ctrl+X Y/`/copy-last`按最新Assistant typed TextPart复制可见回答；Python3.12/3.13各1135项回归及两版本wheel PTY通过。 |
| 2026-08-08 | 完成A122–A124：长会话使用缓存分行+虚拟UIControl，10000行×1000更新从7.457s降至0.0055s；root Application增加一次reset和二次Rich降级；发布门改为隔离依赖/credential-free/resize并在Linux Python3.12与3.13各完成1133项回归和source-external wheel真实PTY。 |
| 2026-08-08 | 完成A121：拆除idle full-screen与run Rich Live的生命周期断点，输入、stream/tool/retry、命令和permission/question selector统一进入一个长期prompt_toolkit Application，并让theme/sidebar/mouse原位热更新；124项组合、Ruff、1128项完整回归及source-external wheel单次alternate-screen PTY通过。 |
| 2026-08-08 | 完成A119/A120：full-screen transcript改为清理控制序列后的Rich Markdown formatted text，并让sidebar消费Todo、已有MCP runtime与workspace隔离LSP状态且不启动进程；56项组合、Ruff与1125项完整回归收口。 |
| 2026-08-08 | 完成A115–A118：默认idle CLI增加full-screen transcript/composer，补齐单turn详情、自适应sidebar和保持child worktree/tool owner的interactive follow-up route；96项组合、Ruff、1122项完整回归和source-external wheel PTY收口。 |
| 2026-08-08 | 完成A114：按InfCode Task→child route源码链增加精确owned child list/load与`/subagents` picker/transcript；保持父Agent/worktree不变并拒绝路径别名；97项组合与1120项完整回归收口。 |
| 2026-08-08 | 完成A111–A113：补齐typed Assistant message终端consumer、task child层级实时投影，并用同一composer内双Ctrl+C状态消除真实PTY竞态；更新后的source-external wheel真实验证composer/slash/退出无Traceback；65项组合与1118项完整回归收口。 |
| 2026-08-08 | 完成A110：按Kilo remapChildren链递归克隆fork引用的task child，同时为NZ独立worktree补齐新所有权、changed/deleted overlay、active拒绝和失败回收；故障注入验证来源worktree不受中途失败影响；67项组合、静态检查与1113项完整回归收口。 |
| 2026-08-08 | 完成A109：让child复用顶层transient分类、Retry-After/指数退避与取消感知等待，并在同一Assistant持久typed RetryPart后恢复；65项组合、静态检查与1109项完整回归收口。 |
| 2026-08-08 | 完成A108：补齐child请求返回前的Provider failure owner，使超时、取消和API异常同样持久Assistant/StepFinish/typed error/endState；64项组合、静态检查与1108项完整回归收口。 |
| 2026-08-08 | 完成A107：将自定义child loop从裸SDK消息升级为与顶层共用的Message/Part/SessionProcessor生命周期，补齐parent/path/time/model/usage/cost、step/tool状态和最终endState；66项组合、静态检查与1107项完整回归收口。 |
| 2026-08-08 | 完成A106：按InfCode默认Session title→first-real-User fallback→set-if-default源码链，补齐100字符确定性标题、synthetic过滤和手工rename保护；80项组合、静态检查与1107项完整回归收口。 |
| 2026-08-08 | 完成A105：按当前InfCode `Session.fork`/`getForkedTitle`源码重建fork Message/Part引用图和标题递增语义，timeline改用parent图优先；同时撤销把普通fork误建模为task child的错误实现；81项组合、静态检查与1105项完整回归收口。 |
| 2026-08-08 | 完成A100–A104：补齐User time/Agent/model/variant、Assistant mode/agent/path/variant、typed RetryPart与turn-final endState；修正stats消息时间和child epoch边界；相关组合与两次完整回归最终1102项通过。 |
| 2026-08-08 | 完成A099：按InfCode Assistant parentID/time→processor finish/cleanup源码链，在请求前持久真实user lineage/created，由统一finish owner写completed；旧Session按synthetic provenance与Part证据迁移；166项聚焦、静态检查与1099项完整回归收口。 |
| 2026-08-08 | 完成A098：按InfCode Assistant model/provider/tokens/cost→CLI stats源码链，为每条Assistant固化模型身份和稳定token投影；新增顶层+child Session统计、父总cost/模型step cost去重、unknown/unattributed费用边界及`/stats [days]`；187项聚焦、静态检查与1098项完整回归收口。 |
| 2026-08-08 | 完成A097：按InfCode task cost-before/after→cost-propagation串行delta→processor reconcile源码链，让前台child持久累计usage/cost、resume只传播增量，父Assistant合并父+子而StepFinish只保留父本步；并修复background terminal/result可见性竞态；172项聚焦、20次竞态重复及1095项完整回归收口。 |
| 2026-08-08 | 完成A096：按InfCode Session.getUsage→providerCost/models.dev pricing→processor finish-step源码链，补齐互斥token口径、cache/reasoning/over-200K价格、Provider账单优先级及Assistant/StepFinish/HTTP cost投影；126项聚焦、静态检查与1092项完整回归收口。 |
| 2026-08-08 | 完成A095：按InfCode fromError/unknownIdentity→processor halt源码链，让stream/non-stream最后Provider exception经LLMResult保留auth/API/unknown身份、status/retryable、headers/body和class/code；133项聚焦、静态检查与1085项完整回归收口。 |
| 2026-08-08 | 完成A094：按InfCode Assistant schema→fromError→SessionProcessor updateMessage→TUI consumer源码链，补齐assistant finish、七类typed error、legacy迁移、HTTP恢复投影和`message.updated`实时事件，并对错误映射敏感字段脱敏；148项聚焦、静态检查与1083项完整回归收口。 |
| 2026-08-08 | 完成A093：按InfCode Retry schedule→SessionStatus retry→TUI consumer源码链，将NZ已有durable RetryPart投影为attempt/倒计时/错误摘要瞬态状态，并在后续真实进度或run终态清除；127项聚焦、静态检查与1079项完整回归收口。 |
| 2026-08-08 | 完成A091/A092：按InfCode Session.delete→Worktree.remove→session.deleted和TUI ToolPart running consumer源码链，补齐持久Session/worktree物理删除、活跃run原子拒绝、CLI双确认，以及既有Part驱动的终端瞬态进度；119项定向、真实PTY、静态检查与1078项完整回归收口，未清理已有历史数据。 |
| 2026-08-08 | 完成A090：按当前InfCode guardEmptyToolCalls→SessionProcessor cleanup源码链，为两种tool-call finish拼写增加无ToolPart降级stop的持久终态守卫；113项聚焦、静态检查与1070项分片完整回归收口。 |
| 2026-08-08 | 完成A089：按当前InfCode Session.getUsage→finish-step tokens与各Provider SDK usage映射源码链，补齐reasoning/cache read/write从协议、LLMResult、trace、assistant usage到StepFinish的详细链；233项聚焦、静态检查与1068项分片完整回归收口。 |
| 2026-08-08 | 完成A088：按当前InfCode ThinkTagDemux→processor text/reasoning event→tool前/cleanup flush源码链，实现逐chunk leading `<think>/<thinking>`状态机、non-stream共用分流和durable Part一致性；237项聚焦、静态检查与1067项分片完整回归收口。 |
| 2026-08-08 | 完成A087：按当前InfCode finish-step→lengthWarning/providerFinishError源码链，补齐Anthropic/Gemini/Responses finish与累计usage producer，再接Session ignored warning/error consumer；阻止length下未执行工具副作用；252项聚焦、静态检查与1058项分片完整回归收口。 |
| 2026-08-08 | 完成A086：按当前InfCode Model.id→api.id/npm/url→getLanguage源码链拆分逻辑与wire模型身份；registry保留api/adapter/endpoint，Agent capability/Session维持logical ID，全部主/规划/记忆/子Agent请求统一使用wire ID；169项聚焦、静态检查与1051项分片完整回归收口。 |
| 2026-08-08 | 完成A085：按当前InfCode models metadata→resolveSDK→getLanguage源码链，为NZ增加显式选择后才导入的Python Provider entry point；补齐无执行发现、内置防覆盖、重复所有者、API版本/contract/初始化归因及workspace selection→AgentLoop真实消费；133项聚焦、静态检查与1048项分片完整回归收口。 |
| 2026-08-08 | 完成A084：按当前InfCode resolveTools.execute→stream processor tool-call/result/error→finish-step源码链，将本地ToolExecutor桥接进Provider stream生命周期；补齐尾usage协调、completed ToolPart不倒退、副作用后stream错误禁止重试、bridge取消/stream close及LLM/tool等待拆分；221项组合、静态检查与1039项完整回归收口。 |
| 2026-08-08 | 完成A083：按当前InfCode Ripgrep.Service源码边界，将JSON search与完整event union decoder下沉runtime，并让files/search共用唯一Popen、bounded queue、stderr、deadline、取消和settlement owner；补齐begin/end/summary Stats schema、code1丢弃rows与code2 partial；77项聚焦、静态检查与1036项完整回归收口。 |
| 2026-08-08 | 完成A082：按当前InfCode SkillTool→共享Ripgrep.files→filter SKILL.md→take10源码链，将A080 files producer提升为runtime单一owner；Glob与Skill共用rg进程、deadline、取消、有界stream及fallback，修正follow=false symlink文件；73项聚焦、真实rg语义、静态检查与1032项完整回归收口。 |
| 2026-08-05 | 完成A081：按当前InfCode GrepTool→Ripgrep.search JSON union→code 0/1/2→stat/mtime→100 row/2000字符→ToolOutput源码链重写NZ grep；默认切换matching lines，保留files/count/context兼容投影；64项聚焦、真实本仓rg smoke、静态检查与1026项完整回归收口。 |
| 2026-08-05 | 完成A080：按当前InfCode GlobTool→Ripgrep.files→take101→stat→mtime sort→ToolOutput源码链重写NZ glob producer；加入path、absolute-in-workspace、files-only、系统rg有界stream、30秒/取消收口、fallback globset与真实rg顺序验证；54项聚焦、静态检查与1016项完整回归收口。 |
| 2026-08-05 | 完成A079：按当前InfCode shared/state/index/routes/watch源码链对齐root instruction file状态与控制面；实现global/project原子0600状态、默认启用/损坏告警、list/create/enable/delete、runtime真实过滤、HTTP routes/client及trace；52项聚焦、静态检查与1010项完整回归收口。 |
| 2026-08-04 | 完成A078：核对并纠正当前InfCode MCP无ctx.abort、DeepMap非本地repo-intel同构产品；按Skill正文→base URI→Ripgrep 10文件sample→ToolOutput源码链，为NZ load_skill增加资源锚点、metadata与cooperative scan cancel；163项组合回归、静态检查与1004项完整回归收口。 |
| 2026-08-04 | 完成A077：先纠正当前InfCode LSP未消费ctx.abort的旧候选，再按Task `ctx.abort` listener→promptOps.cancel child Session源码链，将A075 event接入前台/后台subagent、nested tools、Provider close/poll、rollback和durable cancelled state；110项组合回归、静态检查与1001项完整回归收口。 |
| 2026-08-04 | 完成 A076：按 InfCode Grep/Glob `ctx.abort`→Ripgrep signal→`raceAbort` scoped process源码链，为系统grep、Python逐行fallback与glob逐路径扫描接入A075 per-call event；取消终止并结算进程、不返回partial结果；67项聚焦、静态检查与998项完整回归收口。 |
| 2026-08-04 | 完成 A075：按 InfCode `Tool.Context.abort`→Read/Bash consumer→processor interrupted ToolPart源码链，为每个异步工具调用增加隔离 cooperative event；覆盖全部 scheduler分支，Bash终止进程组，PDF终止 `pdfinfo`/`pdftotext`且不留迟到cache；38 项聚焦、静态检查与 993 项完整回归收口。 |
| 2026-08-04 | 完成 A074：重新核对当前 InfCode并撤销错误 nested候选；按 `InfcodeSessionInstruction`→`applyInstructionBudget`→`renderInstructionsReminder`→first-user/system fallback源码链，补齐三类预算状态、UTF-8边界、tracked/private标签、wrapper转义和独立规则注入；180 项组合回归、静态检查与 988 项完整回归收口。 |
| 2026-08-04 | 完成 A073：按 InfCode `ReadTool.miss/list/isBinaryFile/readLines/warm`→`TextStream.withFallback` 源码链，对齐本地 text/directory Read 的 2000 行、50 KiB、长行、严格 offset、binary/BOM/legacy、目录分页、suggestions、ToolOutput和 bounded LSP warm；129 项组合回归、静态检查与 981 项完整回归收口。 |
| 2026-08-04 | 完成 A072：按 InfCode `ReadTool`→`DocumentConvert.read`→`DocumentPdfPages`→page-aware sidecar 源码链，把 PDF/DOCX 转换接入 `read_file`，增加 pages/offset/limit、20 页上限、长 PDF 显式分页、越界检查、XML/metadata 和页范围 revision cache；146 项组合回归、静态检查与 972 项完整回归收口。 |
| 2026-08-04 | 完成 A071：按 InfCode document FilePart→`convertUserTurnDocuments`→assistant document_read TextPart→MessageV2 source-user reinjection 源码链，增加 PDF/DOCX 分流、有界 schema、DOCX 标准库解析、可选 Poppler PDF、Session sidecar、源指纹、逐项失败/幂等和可结算取消；111 项聚焦、真实 DOCX/PDF smoke、静态检查与 966 项完整回归收口。 |
| 2026-08-04 | 完成 A070：按 InfCode `describeReadToolResult`→`describeReadToolImages`→completed ToolState output/metadata 源码链，为非视觉模型补齐 `read_file` 图片描述；XML hint、`metadata.imageDescribe`、原附件、逐项失败和取消后原 Read 结果落盘形成单一闭环；99 项聚焦、静态检查与 955 项完整回归收口。 |
| 2026-08-04 | 完成 A069：按 InfCode `describeUserTurnImages`→vision describe→assistant TextPart metadata→MessageV2 source-user reinjection 源码链，为非视觉主模型增加独立视觉 Provider preflight、逐图失败、取消终态、Session 幂等恢复和 XML hint；补齐常见视觉 capability；102 项聚焦、静态检查与 949 项完整回归收口。 |
| 2026-08-04 | 完成 A068：按 InfCode terminal file→resolveUserPart→user FilePart→MessageV2 capability filter→同 user turn Provider media 源码链，将文本附件与图片分流，新增 durable FilePart、数量/10 MB 降级、Session 恢复、连续 user media 合并及 OpenAI/Responses/Anthropic/Gemini same-turn 请求；97 项聚焦、静态检查与 942 项完整回归收口。 |
| 2026-08-04 | 完成 A067：按 InfCode WebFetch URL/permission→5 MB bounded HTTP→HTML format/image ToolOutput→A066 Provider replay 源码链，新增标准库 webfetch、30/120 秒超时、声明/流式/解压三层边界、gzip/deflate、redirect/IDNA/loopback proxy 安全与可配置 permission；122 项聚焦、静态检查与 937 项完整回归收口。 |
| 2026-08-04 | 完成 A066：按 InfCode Read/MCP execute wrapper→ToolOutput attachments→completed ToolPart→MessageV2 capability filter→Provider media 请求源码链，接通四类图片、读取前 10 MB 守卫、MCP image/resource blob、安全去重与 OpenAI/Responses/Anthropic/Gemini replay；83 项首轮、116 项最终聚焦、静态检查与 931 项完整回归收口。 |
| 2026-08-04 | 完成 A065：按 InfCode question tool request ID→Question service pending→QuestionPart/QuestionSummaryPart 源码链，让工具、HTTP broker 与 Session 共用身份；answer completed+summary，dismiss/cancel/timeout terminated，崩溃恢复结算不可恢复 pending；38 项定向、151 项核心聚焦、静态检查与 920 项完整回归收口。 |
| 2026-08-04 | 完成 A064：按 InfCode `Tool.Context.metadata`→Bash chunk producer→running ToolPart→Session/SSE consumer 源码链，增加 execution-local reporter/call ID、并行安全持久更新、Bash 增量 output preview 与最终 exit/workdir/truncated metadata；18 项故障边界、15 项最终聚焦、静态检查与 916 项完整回归收口。 |
| 2026-08-04 | 完成 A063：按 InfCode runLoop `compactionAttempts`→两个 guard producer→turn error consumer 源码，为 pre-send usage/request compact 与 reactive overflow 建立单一三次 owner；第 4 次摘要前持久 exhaustion，manual compact 独立，并修复 preserved tail 旧 usage 重复触发；161 项聚焦、静态检查与 912 项完整回归收口。 |
| 2026-08-04 | 完成 A062：按 InfCode compaction-payload-recovery→compaction fallback→input-expansion 源码链，为摘要请求增加窄 overflow 分类、持久 tool/expansion strip、payload shrink guard、一次 retry、oversized/aggregate placeholder 和无边界显式错误；187 项聚焦、静态检查与 909 项完整回归收口。 |
| 2026-08-04 | 完成 A061：按 InfCode snapshot.patch→PatchPart→Session summary/diff 源码链，将 step start/finish 快照转为非空 PatchPart、turn 轻量 diff 和 Session 有界完整 diff；接通 compaction、磁盘 artifact、timeline、idle snapshot 与 `/session/:id/diff`，以 174 项聚焦、静态检查和 903 项完整回归收口。 |
| 2026-08-04 | 完成 A060 当前真实 producer 范围：按 InfCode QuestionTool→ToolOutput→SessionProcessor ToolPart 源码，为 answer/dismiss 增加 str-compatible title/answers/dismissed metadata，确认 dismiss completed 后继续；同时把 Responses/Gemini tool-call Provider metadata 从 stream delta 持久到 Session projection；157 项聚焦、静态检查与 899 项完整回归收口，并保留 GUI QuestionPart、running metadata、Suggestion/providerExecuted/MCP attachment 差距。 |
| 2026-08-04 | 完成 A059 主请求恢复链：按 InfCode processor `needsCompaction`→prompt compact consumer→三次 guard 源码，将 Provider context overflow 从普通 400/JSON 诊断中分离；stream/non-stream 产生 typed outcome，结算失败 step、持久降级 synthetic input expansion、生成 overflow summary 后恢复 turn；82 项定向、196 项核心聚焦、静态检查与 897 项完整回归收口。 |
| 2026-08-04 | 完成 A058 第一阶段：核对 InfCode resolveTools(execute)→AI SDK stream→SessionProcessor tool-result/error→continue/stop 真实边界；为 NZ ToolExecutionResult 增加 typed permission denial，统一 processor settle/outcome，默认拒绝后 checkpoint 并停止当前 turn，保留显式 continue-on-deny；138 项核心聚焦、静态检查与 893 项完整回归收口。 |
| 2026-08-04 | 完成 A057 第一阶段：按 InfCode processor→snapshot store→SessionRevert 生产链，为普通 Agent 增加真实 step-start/finish workspace snapshot、无 Git 内容寻址 manifest/blob、冲突预检原子 transition、message revert/unrevert 与 `/undo`/`/redo` 消费；真实本仓首次/增量 capture 0.67s/0.21s，158 项核心聚焦、静态检查与 889 项完整回归收口。 |
| 2026-08-04 | 完成 A056：将 OpenAI-shaped text/reasoning/tool-call delta 直接接入 durable SessionProcessor；partial JSON、晚到 call ID、完整 call 对账以及 stream retry/cancel/error 都更新并结算同一个 ToolPart；25 项定向、130 项核心聚焦、静态检查与 880 项完整回归收口。 |
| 2026-08-04 | 完成 A055 第一阶段：按 InfCode prompt→processor→MessageV2→retry 生产链，将请求前 step-start、reasoning、tool pending/running/completed/error、retry 和 step-finish 写成真实 Session parts；取消/失败确定性收尾，retry 分类与 Retry-After 生效，并按模型族强化 prompt contract；146 项核心聚焦、静态检查与 875 项完整回归收口。 |
| 2026-08-04 | 完成 A054：直接移植 InfCode InputExpansion tag/applyBudget/compactStored 与 CompactionPart/tool compact marker 行为，接通 `/attach` 和 inline `@file` 真实入口，分离自然文本与系统展开内容，并增加唯一 archive/head IDs/tail boundary/Session round-trip；177+51+25 项聚焦、静态检查与 869 项完整回归收口。 |
| 2026-08-03 | 完成 A053：深入核对 InfCode ContextBudget→preflight→overflow→compaction→filterCompacted 与 instruction/rules 生产链，纠正 A052 只对齐比例公式的过度判断；实现 soft 清理/hard 摘要、Provider usage、模型预算 tail/split、AGENTS/CLAUDE/rules、动态低优先级 memory、全 permission mode 提取和稳定 message-ID cursor；145 项聚焦、静态检查与 861 项完整回归收口。 |
| 2026-08-03 | 完成 A052：按 InfCode SessionPrompt 非 tool-call finish 直接结束、SessionProcessor first-token metrics 与 ContextBudget 生产链，移除 NZ 默认 verification/reflection completion gate，补齐模型 duration/TTFT/input growth/retry/child span trace；116+59 项聚焦、真实同题无 gate/reflection 回放和 855 项完整回归收口。 |
| 2026-08-03 | 完成 A051：核对 InfCode keybind 与 prompt input consumer，修复 NZ-Coder 永久捕获 Ctrl+C 后无法退出的问题；实现非空清空、空输入一秒双击退出、fallback 同行为并保留运行中安全取消，以 58 项聚焦、真实 PTY 退出码 0 和 854 项完整回归收口。 |
| 2026-08-03 | 完成 A050：从 InfCode app/prompt/session command registry、transcript 和 clipboard 生产源码提取差分，补齐 rename/copy/export/skills/mcps/variants/editor/exit 本地命令与持久状态；102 项聚焦、真实 PTY 命令链和 849 项完整回归收口，同时明确不伪造 share/org/agent-profile/OpenTUI-only 能力。 |
| 2026-08-03 | 完成 A049：回到 InfCode `prompt.ts`/`max-steps.txt`/`processor.ts`/session TUI 生产源码，撤回未经佐证的 read-episode 与语义失败熔断，补齐最后 step 文本收尾、精确三次 doom-loop 权限和 inline/Bash-block 工具投影；78 项聚焦、真实 DeepSeek 两 step PTY 与 843 项完整回归收口。 |
| 2026-08-03 | 完成 A048：以 InfCode command/model/provider/theme/prompt/session/keybind 生产链为参照，补齐 Ctrl+P 分类命令面板、leader/编辑器/文本粘贴、主题/mouse/tool details、recent/favorite/F2 模型控制、掩码 Provider connect、ContextVar live credential、一次性安全附件和粘贴卡；69 项聚焦、真实 PTY 与 838 项完整回归收口，并更新项目记忆 contract。 |
| 2026-08-03 | 完成 A047：用真实 50-turn/105-call 审查故障证明固定 8K/最后 3 条 micro-compact 导致证据颠簸，改为模型预算与最近两个真实用户回合保护；增加跨调用只读 episode 熔断、成功写入代际重置和 synthetic user message 语义，以 110 项聚焦、真实序列离线重放及 821 项完整回归收口，并更正“Agent Core 已对齐”的过度结论。 |
| 2026-08-03 | 完成 A046：从真实 Ctrl+C traceback 反查取消边界，修复同 REPL 恢复、线程 worker settle、晚到写事务回滚、单只读 event-loop 阻塞、结果回调异常事务悬挂和异步 slash 取消；重新把 A001–A045 分成当前边界可用、部分对齐、外部互操作和 deferred 证据，并以 814 项分组全量回归收口。 |
| 2026-08-03 | 完成 A045：直接核对 InfCode prompt autocomplete、command dialog 和 model dialog 调用链，补齐行首 slash popup、alias/description、Enter action、`/models` picker/按需 discovery 与 `/mode` 风险选择；41 项 CLI 聚焦、受控 VT render 和 806 项完整回归通过。 |
| 2026-08-03 | 完成 A044：根据真实启动反馈修复 PromptSession 固定 bottom toolbar 造成的整屏空白和空 Enter 提示符堆叠，增加宽度感知的内联 composer、状态标题、多行边界与提交后闭合边框；38 项 CLI 聚焦、真实 PTY 和 803 项完整回归通过，并更正此前把功能闭环等同于产品级布局的结论。 |
| 2026-08-03 | 完成 A043 本地发布收口：修复 wheel 场景 workspace dotenv 和 Provider-specific credential 首启问题，增加 non-overwrite 0600 `init`、secret-free offline `doctor`、package-owned bundled skill、source-external wheel smoke 和明确证据矩阵；45 项发布聚焦、真实 wheel 安装和 802 项完整回归通过。 |
| 2026-08-03 | 完成 A042 CLI 交互完整收口：阻塞 serial tool 移入 ContextVar-aware worker，以线程安全 bridge 复用 fuzzy selector 完成 Permission once/always/reject 和 Question 单选/多选/custom/dismiss，并让替换 Agent 自动重绑；121 项交互/调度/HTTP 聚焦、真实 PTY 工具线程闭环和 793 项完整回归通过，CLI core 转入 frozen。 |
| 2026-08-03 | 完成 A041 第一阶段：以自研 value/label selector 替换 radio shortcut，增加确定性 fuzzy ranking、实时过滤、循环/翻页移动、14 行有界窗口、单 Enter 返回和 Esc 取消；以 42 项聚焦、真实 PTY `sec`→`Second session` 和 782 项完整回归验证。 |
| 2026-08-03 | 完成 A040 第一阶段：增加单一 awaited keyboard selector 和 sync/async 命令兼容层，将 Session/model/fork 选择接回既有安全 owner lifecycle；以 36 项聚焦、真实 PTY selector 和 776 项完整回归验证，并明确 fuzzy、单 Enter 与 Permission/Question 尚未完成。 |
| 2026-08-03 | 完成 A039 第一阶段：增加真实 user-turn timeline、active/saved Session metadata 表和同 workspace 完整回合 deep-copy fork，明确 conversation/filesystem 边界并实现新 Agent 构造失败回滚；以 39 项聚焦、真实 PTY 表格和 769 项完整回归验证。 |
| 2026-08-03 | 完成 A038 第一阶段：以 SessionEventBus 为唯一事实源增加 tool started/completed、结构化安全卡片、run settlement、changed-file summary、Permission/Question 卡片和异常后继续 REPL；以 102 项聚焦、真实终端卡片和 761 项完整回归验证。 |
| 2026-08-03 | 完成 A037 第一阶段：以 prompt_toolkit 接管交互 TTY，增加 async 多行编辑、私有持久历史、slash/session/model/file 补全、动态状态栏、非 TTY 回退、`/keys` 和同 Session `/model` 替换/失败回滚；以 69 项聚焦、真实 PTY 和 756 项完整回归验证。 |
| 2026-08-03 | 完成 A036：将 A028 时点差距表明确标成历史快照，按 frozen core、deferred evidence、interoperability 和 consumer-driven product work 重建当前矩阵；新增 release baseline 并同步 README/architecture，以 5 项 reader checks、4 个离线 CLI 冒烟和 745 项完整回归验证。 |
| 2026-08-03 | 完成 A035：以 immutable ExtensionDescriptor 统一 Skill、Hook、optional tool pack 和 MCP server 的 identity/source/scope/trust/status/capabilities/effects/permissions/lifecycle，增加 strict source failure isolation、pre-load effect manifest 和 secret-free extensions list/status CLI；以 10 项定向、100 项聚焦、本地真实 CLI 和 740 项完整回归验证。 |
| 2026-08-03 | 完成 A034：证明 Dodo/PySide production caller graph 为空，确认 memory/EventBus/HTTP Session/background agent/trace 能力已内化，物理删除 39 个平行控制面、worker、客户端、专属测试和安装文档文件；保留可能含用户数据的 `.dodo-server/memory.db`，以 1 项架构边界、75 项核心聚焦和 730 项完整回归验证。 |
| 2026-08-03 | 完成 A033：新增 models.dev-compatible 显式 sync、5 分钟 freshness、workspace flock、10 MB/500 provider/50000 model 上限、非法刷新保留旧 snapshot，并将精确 context/output/tools/reasoning/temperature 按 registry→本地 catalog→环境 override 合并进 Agent capability；以 10 项定向、124 项聚焦、真实本地并发 registry 链路和 773 项完整回归验证。 |
| 2026-08-03 | 完成 A032：新增 OpenAI-compatible/Responses、Anthropic、Gemini 显式模型发现，有界分页与无凭据 0600 cache，增加 models list/refresh/select/current/reset，并让 workspace model/variant 选择真正进入 AgentLoop、memory、banner、session metadata 和 status；以 9 项定向、114 项聚焦、真实 loopback 多协议链路和 763 项完整回归验证。 |
| 2026-08-03 | 完成 A031：新增 user/project/environment MCP 配置覆盖、project-local command SHA-256 用户信任、CLI list/trust/untrust、运行中 added/removed/changed reconcile、非法配置保留健康 generation，以及 Streamable HTTP 到 same-origin legacy SSE fallback；以 7 项定向、185 项聚焦、两个真实本地协议链路和 754 项完整回归验证。 |
| 2026-08-03 | 完成 A030：新增 Session-owned 后台写子 Agent 管理器、20 任务/4 并发上限、非重叠 scope reservation、Git/current-dirty snapshot 与无 Git copy fallback、持久 status/cancel/interrupted、精确父审查和 baseline hash 后事务应用；以 9 项定向、121 项聚焦和 747 项完整回归验证。 |
| 2026-08-03 | 完成 A029：将 Repo Map 的进程内缓存升级为 workspace 隔离 SQLite symbol/reference index，新增重启复用、stale 安全清理、精确 Python 引用查询和成功事务后的增量替换/删除；以 7 项索引、105 项聚焦和 738 项完整回归验证。 |
| 2026-08-03 | 完成 A028：新增独立 OpenAI Responses/Codex provider，覆盖 input item、function call/result、流式事件、加密 reasoning replay、专属凭据和统一 Loop 归一化；以官方 openai SDK 本地序列化、7 项定向、129 项聚焦和 731 项完整回归验证，并把下一 P0 收敛为持久增量代码索引。 |
| 2026-08-03 | 完成当前差距再审计：纠正最早差距表中已经过时的 runtime、context、HTTP 和 MCP 判断；将当前差距收敛为 Provider 原生协议、持久代码索引、MCP 配置互操作、写子 Agent 编排、Dodo 物理收敛、客户端生态与可复现评测证据，并明确核心 Agent 与 App/IDE 产品广度的优先级边界。 |
| 2026-08-02 | 完成 A027：增加 MCP OAuth protected-resource/authorization-server discovery、dynamic registration、PKCE/state loopback callback、authorization-code exchange、URL-bound 0600 store、refresh single-flight、runtime needs_auth/token invalidation 和 CLI auth/status/logout；明确 challenge parsing、跨进程 refresh、revocation、HTTP auth API 与公网互操作尚未完成。 |
| 2026-08-02 | 完成 A026：增加 stdlib MCP Streamable HTTP client、JSON/SSE response、GET notification stream、Session-ID 复用与 DELETE、远程配置和环境变量 header credential；明确禁用代理/重定向并限制 HTTP/URL/header/响应大小，OAuth、旧 SSE fallback、project reconcile 和第三方 live smoke 尚未完成。 |
| 2026-08-02 | 完成 A025：增加 workspace-bounded exact provider/model catalog、active-model reasoning variant、OpenAI-compatible/Anthropic/Gemini 参数映射、命名兼容 provider 和 Agent-owned capability snapshot；明确 models.dev、Responses API、model picker、per-provider credentials 与 live smoke 尚未完成。 |
| 2026-08-02 | 完成 A024：将 MCP 从 per-run 顺序 stdio wrapper 提升为 Agent/Session-owned runtime，增加并行/后台启动、共享 readiness generation、connect/disconnect/reconnect、tools/prompts/resources cache、三类 list-changed（含 handler 注册前有界 replay）、JSON-RPC 错误码分类、失效 generation 退休、线性化生命周期事件、live ContextVar tool provider、startup generation guard 和 owner 异常路径清理；明确 remote HTTP/OAuth/project config 仍未完成。 |
| 2026-08-02 | 完成 A023：修复 bounded subscriber 静默 drop（含 filtered close 竞态），增加无 ID `server.event_gap`、snapshot-first 自动重同步、原子 accepted/terminal run 状态、authoritative Session-ID commit 与 restart `interrupted`；以真实 loopback 和持久化失败测试闭合 snapshot→SSE→settled，并冻结 HTTP 默认开发范围。 |
| 2026-08-02 | 完成 A022：参考 InfCode `MessageV2.WithParts` 与持久 message/part projection，为 NZ-Coder 增加向后兼容的 message/text-part metadata、旧会话确定性 identity 迁移、idle `/snapshot`、EventBus checkpoint anchor 与 410 后 strict-after resync；明确 running snapshot、完整 Part 类型和跨文件磁盘事务仍未实现。 |
| 2026-08-02 | 完成 A021：对齐 InfCode assistant text part delta/update/remove，为 HTTP streaming 增加稳定 message/part identity 与 abort attempt retirement；并在明确 InfCode server 尚未 wired SSE ID 的前提下，实现 NZ-Coder 的 SSE `id:`、Last-Event-ID strict replay、410 过期、按记录阈值压缩且只开放连续安全后缀的 JSONL journal、跨服务重启 cursor 与标准库 client 有限自动重连。 |
| 2026-08-02 | 完成 A020：对齐 InfCode Session workspace identity 与 instance routing 的本地不变量，为 HTTP service 增加启动者登记 workspace、稳定选择器 ID、重叠 root 拒绝、Session 固定路由、per-workspace gate、workspace prompt state、有限重启扫描、严格持久校验和 dormant 懒恢复；明确 registry 不是 OS sandbox，且未复制 remote workspace/proxy/control plane。 |
| 2026-08-01 | 完成 A019：对齐 InfCode permission/question deferred 生命周期，为 HTTP Session 增加 pending list、once/always/reject、question reply/reject、asked/replied/rejected 事件、可配置 timeout 与 run-generation abort 门；固定先发终态事件再唤醒 Agent 的因果顺序。 |
| 2026-08-01 | 完成 A018：将 A017 EventBus 接到真实 loopback HTTP/SSE transport，实现 Bearer-auth Session CRUD、workspace 串行 run、manager settled 提交屏障、202 异步执行、abort/dispose、断开清理和标准库 client；显式拒绝 remote bind、authenticated browser Origin 与交互 ask，并让 localhost client 绕过环境代理。 |
| 2026-08-01 | 完成 A017：以 InfCode instance Bus 和 SSE event route 为参照，实现实例级 SessionEventBus、稳定 envelope、有界订阅、过滤/回放、ContextVar 和 SSE framing；将通用 memory store/sync 注入内化到 core，并让 Dodo 配置、自动 trace、默认脚本和外壳退出主架构，明确不自动外传敏感事件。 |
| 2026-08-01 | 完成 A016：以当前本地 InfCode MCP service 为参照，手写 local stdio JSON-RPC client，接入严格配置、per-run 生命周期、ContextVar 动态工具、effect/权限和子 Agent 隔离；显式区分 MCP 外部 write 与本地可事务写入，并保留 HTTP/OAuth/resources/prompts/list-change 等第二阶段差距。 |
| 2026-08-01 | 完成 A015：以本地 InfCode 的 Provider.Model、SystemPrompt、ProviderTransform 与 ContextBudget 为参照，为已有三种 adapter 增加 immutable capability registry，并接入模型预算、prompt family、tools/stream/reasoning、GPT-5 token 字段、子 Agent 和 trace；明确静态 family rules 不等于 models.dev 或完整 provider 生态。 |
| 2026-08-01 | 完成 A014：对齐 InfCode instance context 的隔离语义，以 ContextVar 提供 workspace、turns、timeout、parallel limit 和 broad-test guard 的嵌套作用域；迁移主/子 Agent、Dodo、SWE 与 evaluation 入口，并用线程、fork 和 AST 防线验证生产代码不再写模块级 config。 |
| 2026-08-01 | 完成 A013：对齐本地 InfCode 工具 start/end 与 duration/status 指标，为 NZ-Coder 增加 per-call duration/queue wait、batch/segment actual peak、serial barrier drain wait、streak reset 原因和本地 trace/runtime 汇总；明确 batch/barrier/reset 属于 NZ-Coder 调度增强。 |
| 2026-07-31 | 完成 A012：以本地 InfCode 的 diff/snapshot、Local Review 和 minimal-intrusion 原则为参照，为普通 AgentLoop 增加基于 ChangeTracker 当前快照的公开 API/范围风险信号、patch 指纹去重、可选保守 replan 和 Reviewer limitation；明确该自动 replan 是 NZ-Coder 增强，不伪称 InfCode 具有同一模块。 |
| 2026-07-31 | 完成 A011：以当前本地 InfCode 源码快照为参照，实现可审批的 Plan/Build 状态机、专属原子计划文件、动态只读提示、审批期间编辑检测和批次后权限切换；同时纠正此前把 NZ-Coder 父仓库提交误当作 InfCode 来源版本的文档说明。 |
| 2026-07-31 | 完成 A010：对齐当前本地 InfCode 源码快照中的结构化 question 工具，为 NZ-Coder 增加 1–4 题 schema、单选/多选/自定义回答、CLI renderer 暂停恢复、会话级回调隔离和无头快速失败。 |
| 2026-07-30 | 收敛 A009 范围：撤回 formatter/fixer 通用拦截和动态 Bash 写入事务扩展，明确它们不属于 InfCode 验证对齐；保留三层计划、命令级门禁、失败目标延续、Bash 验证证据和写后证据代际。 |
| 2026-07-30 | 完成 A008：确认 InfCode 没有通用 frozen-symbol 实现，对齐其三次相同工具调用防线，并为 NZ-Coder 增加 dispatch 前硬阻断、规范化参数比较和保守恢复诊断。 |
| 2026-07-30 | 完成 A007：以显式 read/serial/write 效果元数据替代“非写即并行”，实现连续只读段并行、副作用顺序屏障和动态写工具事务/子代理隔离。 |
| 2026-07-30 | 完成 A006：复核三级记忆系统，持久化 workspace/session 工作状态，并将全量摘要压缩升级为 anchored summary 加近期完整回合保留。 |
| 2026-07-29 | 完成 A005：将 Repo Map 扩展为多语言结构地图，Python 保持 AST，十个非 Python 语言族使用保守声明提取并复用缓存、排序和 LSP probe。 |
| 2026-07-29 | 完成 A004：将 Repo Map 与 LSP workspace symbols 联动，增加可选语义补充、范围过滤、稳定降级和首次空响应短重试。 |
| 2026-07-29 | 完成 A003：对齐 InfCode 分层候选排序原则，为 Repo Map 增加符号、文件名、路径和模糊子序列的稳定相关性排序。 |
| 2026-07-29 | 创建文档；补录 A001 LSP 与写后诊断、A002 Python Repo Map 两项对齐记录；根据无上下文读者测试补充复现命令、环境、诊断 push/pull 和设计边界说明。 |

## A232：统一 Runtime 原提示词逐条补验

- 纠正：A231 的 Main/child 单 Runner 结论成立，但“本轮架构目标完成”不成立；测试通过不等于未消费的抽象已经进入生产。
- 生产服务图：composition 为每个 Agent 构造必需的 model/tool/context/session/event/host/memory/verifier 服务，Runner 不再直接调用四个旧 Loop 私有边界。
- SDK：`AgentDefinition` 的 guardrails、nested `AgentHandoff`、output schema、provider/model/effort 和工具策略会解析为可执行 `AgentGraph`；无法解析的 handoff 在 Provider 启动前失败。
- Child：不再直接实例化 `AgentLoop`，由 `declared_runtime(graph).build()` 构造，与 Main 共用 composition、services、host 和 Runner。
- Ownership：模型 worker/cancellation settlement 与 Memory recall/terminal learning 已移出 `AgentLoop`；同步旧入口只保留兼容 facade。
- Evidence：新增 architecture guards、SDK declaration tests 和 coding/declared profile trace parity。外部 SWE-bench 分数和付费 Provider 互操作不属于本次架构完成声明。

## A233：统一 Runtime 最终 ownership 收口

- 纠正完成口径：此前多次把“阶段测试通过”写成“原提示词全部完成”，根因是没有固定逐条验收矩阵，也没有在每次改动后重跑全量测试；A231 的“完成”表述已由 A232/A233 取代。
- Guardrail：新增必需 `GuardrailRuntime` 端口与 `ProductionGuardrailRuntime`，Runner 的 input/output 以及 ToolRuntime 的 before/after tool 均直接消费该服务，Loop 只保留兼容 facade。
- Input：新增必需 `InputPreflight` 端口，用户图片、PDF/DOCX 和 `read_file` 图片描述进入同一生产服务；保留无完整 service graph 的测试/嵌入兼容路径，但不复制策略实现。
- Transition：新增必需 `AgentTransitionRuntime` 端口，SDK 声明产生的 handoff、Agent-as-tool 返回、terminal signal 与 output schema 唯一修复轮次由该服务执行。
- Tool：将并发判定、角色/系统能力准入、strict 收敛、doom-loop、批次观测迁入 `tool_runtime/policy.py`，将连续 tool result、hook 与 handoff signal 投影迁入 `tool_runtime/result_projection.py`。
- Lifecycle：`ProductionRunLifecycle` 同时拥有初始化和所有终止结算；生产 Runner 不再调用 `host._init_run` 或 `host._finalize_async`。
- Evidence：coding/declared profile 共用 Runner/services 且 trace 顺序一致；child 经 `declared_runtime(graph).build()` 进入同一内核；最终离线验收 `1549 passed`，Ruff、compileall、dependency scan 全部通过。
- 边界：本结论是 Agent Runtime 架构与执行语义收口，不是 SWE-bench 分数、终端 UI 或公网 Provider/MCP 互操作等价声明。`AgentLoop` 仍是较大的兼容/coding host，后续拆 planning/reflection/trace 是维护性工作，不能再反向解释成存在第二套 Agent Core。
# 2026-08-10 — Session-first Runtime Phase 2 design approved

- Re-audited the current source instead of relying on the previous completion
  report. The full suite currently passes (`1549 passed`), but the architecture
  has only one provider/tool turn loop, not yet one complete Session Runtime.
- Confirmed that production `AgentRunner` still receives `AgentLoop` plus a raw
  mutable message list. `RunState` and `FileSessionRepository.load/save` are not
  wired into the production chain, so these abstractions are presently
  test-only/partial.
- Confirmed that Main, Sub, and Background reach the same Runner, while Session
  ownership remains split across AgentLoop, child JSON state, and AgentManager.
- Approved the Session-first strangler direction: InfCodeX supplies the Runner,
  middleware, guardrail, verification, and SDK principles; infcode-dev/OpenCode
  supplies Session, Message/Part, Stream Processor, Compaction, Permission, and
  Child Session principles.
- This entry records design alignment only. No runtime implementation is claimed
  complete. The detailed design is in
  `docs/superpowers/specs/2026-08-10-session-first-runtime-phase-2-design.md`.

## A234：Session-first Runtime Phase 2A 生产接入

### 本阶段解决了什么

上一阶段虽然只有一个 `AgentRunner._run_turns()`，但 Runner 仍直接接收
`AgentLoop + messages`，生产代码没有创建 `RunState`，而
`FileSessionRepository.load/save()` 也只有测试消费者。本阶段没有增加第二套
loop，而是在现有唯一 Runner 外围接入真正的 Session ownership：

```text
AgentLoop compatibility host
  -> immutable RunRequest
  -> SessionRuntime.open
  -> Session-owned transcript + RunContext
  -> existing AgentRunner._run_turns
  -> SessionRuntime.checkpoint/finalize
  -> mirror transcript to legacy caller list
```

### 主要代码变化

- `runtime/session/model.py`：新增安全 Session identity、可恢复 run status、完整
  transcript ownership、parent session、metadata、usage、snapshot 和 dirty state。
- `runtime/session/store.py`：新增 `SessionStore` Protocol 与
  `LegacyJsonSessionStore`，复用原有原子 JSON writer，不建立第二种磁盘格式。
- `runtime/session/runtime.py`：统一 load/create、resume reconciliation、checkpoint
  与 exactly-once Run finalization。
- `runtime/core/run_context.py`：生产 Run 的 active agent、usage、turn/retry/
  compaction counters 和 terminal guard 有了单一 owner。
- `runtime/runner.py`：真实生产入口现在先创建 RunContext；原先 19 个
  `services.sessions.checkpoint(host, messages, ...)` 全部迁到
  `services.session_runtime.checkpoint(run_context, ...)`。
- `runtime/session_processor.py`：新增稳定 message mutation sink。text、reasoning、
  tool state、finish 和 child cost 的稳定变化会通知 Session owner，但不会按 token
  同步写磁盘。
- `state/sessions.py`：旧 JSON payload 以向后兼容方式增加可选
  `parent_session_id` 和 `metadata`。

### 实现中发现并修正的架构事实

1. `completed/error` 是一次 Run 的状态，不是整个 Session 永久关闭。恢复已完成
   Session 后必须允许新的 user activation；只有显式 `Session.close()` 才禁止追加。
2. 旧 `AgentLoop.run()` 的调用者既可能传完整历史，也可能只传本轮新 user
   message。SessionRuntime 现在区分 common-prefix resume 与无 identity 的新 activation，
   同时拒绝带 durable identity 的冲突历史。
3. 初次实现暴露 `session.model -> core.__init__ -> contracts -> run_context ->
   session.model` 循环。修复方式是让 contracts 仅在 `TYPE_CHECKING` 下引用
   RunContext，并停止从 `core.__init__` eager export Session-dependent 类型。
4. 使用 `AgentLoop.__new__` 注入 `_run` 的极简测试没有 service graph。该路径保留
   一个明确的 legacy override；正常构造的 Main/Sub/Background 生产 Agent 仍必须
   使用完整 RuntimeServices。

### 验证证据

- 新增 Session/Store/RunContext/Processor sink 测试均经历红—绿循环。
- 全量：1573 passed。
- 架构专项：58 passed。
- Ruff 与 compileall：通过。
- 离线 runtime smoke：6 个任务顺序稳定，parallel peak=3，speedup=2.60x。
- AST SCC：仍为 5 组，但最大组从 21 个模块降到 20 个。
- Runner checkpoint：旧 SessionRepository 调用从 19 降为 0；SessionRuntime 为 19。

### 尚未完成

- P0：Runner 仍有 27 类、59 处 `host._private` 访问；Context/LLM/Tool ports 尚未
  改成只接收 focused contexts。
- P0：SubAgent/Background 虽经过同一 Runner，但其 parent Session 仍主要来自
  child state JSON，尚未完全改成 Task Tool 创建原生 child Session。
- P1：ToolRuntime 内的 `host._checkpoint_messages()` 仍经过兼容 SessionRepository；
  本阶段只收口了 Runner 自己的 19 个 checkpoint。
- P1：`RunState` 和 `FileSessionRepository` 仍作为兼容 API 存在，删除门槛未满足。
- P1：公共 SDK 仍通过 AgentLoop composition adapter 构造生产 Agent。
- P2：全局 `TOOL_SPECS`/`TOOL_HANDLERS` 与副作用注册仍存在。
- P2：项目仍没有 mypy/pyright 配置，因此不报告 type-check pass。

下一阶段应按顺序处理 focused Runtime contexts、Tool checkpoint、原生 child
Session、Background Session 调度，以及通过消费者清零删除兼容代码。

## A235：Session-first Runtime Phase 2B focused Context 与 Tool checkpoint

### 为什么先迁移 Context

Phase 2A 已让 Session 拥有 transcript，但 `ContextManager` 的接口仍是
`prepare_async(host, messages)`。源码审计发现它只需要 5 类 host 能力，适合作为
host-free Runtime ports 的第一个切口；Tool Runtime 则仍需要约 30 类能力，如果同批
整体替换会把 Context、事务、权限、快照、hooks 和 handoff 多个生命周期绑在一次改动中。

本阶段因此采用两步 strangler migration：

```text
AgentLoop compatibility host
  -> context_from_legacy_host (唯一翻译边界)
  -> immutable ContextExecutionContext
  -> ProductionContextManager

AgentRunner + RunContext
  -> injected async checkpoint(status)
  -> ProductionToolRuntime
  -> SessionRuntime.checkpoint(run_context, status)
```

### 具体变化

- `runtime/core/context.py`：新增不可变 focused context，只包含 workspace、预算快照、
  token projection、compact、stamp 和 trace。
- `runtime/adapters/context.py`：集中承接旧 AgentLoop 私有方法；生产 Context Runtime
  不再知道 AgentLoop。
- `runtime/context_manager.py`：sync/async 两条路径都只消费 focused context，并直接
  使用其 workspace，不再依赖隐式 `current_workdir()`。
- `runtime/runner.py`：在第一个真实 turn 时惰性构造一次 context；零轮次不会索取无用
  能力。stream tool handler 与普通 tool batch 都注入同一个 SessionRuntime checkpoint。
- `runtime/tool_runtime/pipeline.py`：异步 start/interrupted/finish checkpoint 优先 await
  注入边界；活跃 RunContext 缺少注入时 fail closed，只有无 active RunContext 的直接旧
  调用者才能回退 `_checkpoint_messages()`。

### TDD 中发现的兼容事实

首次在 `_run_turns()` 入口立即创建 focused context，导致零轮次极简 Runner host 因没有
tracer 而失败；旧 `AgentLoop.__new__` 测试也可能没有 workdir。根因不是 Context Runtime
需要容忍残缺 host，而是 adapter 构造发生得过早。最终修正为首个 turn 惰性构造，旧
adapter 的 workspace 沿用原有 ContextVar fallback，核心 ContextManager 未增加兼容判断。

### 源码证据与边界

- `ProductionContextManager`：24 处、5 类 host 访问降为 0。
- `execute_batch_async`：3 处直接 `_checkpoint_messages()` 降为 0。
- Runner 两个 `execute_batch_async` call site 均显式提供 `checkpoint=`。
- `execute_batch_sync` 仍保留 3 处旧 checkpoint；它属于直接同步兼容入口。
- async Tool pipeline 除 checkpoint 外仍有 15 类 host 能力；ToolExecutionContext 尚未
  完成，不能把本项解释成 Tool Runtime 全面 host-free。
- Runner 本身的大量 planning、snapshot、hooks、message materialization 私有访问没有
  因本项自动消失，后续必须按 focused port 分批迁移。

### 验证证据

- 新增 9 项行为/架构回归，均经历对应生产行为的红—绿验证。
- 全量：`1582 passed in 98.20s`。
- Model/Tool/Context/architecture 专项：`63 passed in 5.17s`。
- Ruff、compileall、6 个关键模块 import smoke、`git diff --check`：通过。
- 离线并发 smoke：顺序保持，peak concurrency=3，speedup=2.69x。
- 未调用付费 Provider、未运行 SWE-bench、项目仍未配置静态 type checker。

### 下一步

1. 建立 focused ToolExecutionContext，迁移 transaction/executor/policy/result projection。
2. 把 Model Runtime 的 client/capabilities/stream retirement 变成显式 ModelRunContext。
3. 把 Lifecycle/Hook/Snapshot 能力从 Runner 的 host 私有调用迁到服务端口。
4. 让 Task Tool 创建 parent-linked child Session，再迁 Background Session。
5. 消费者清零后删除同步 checkpoint 与 FileSessionRepository compatibility API。

## A236：Session-first Runtime Phase 2C focused ToolExecutionContext

### 本阶段解决了什么

A235 只把 Context Runtime 和 Tool checkpoint 从 AgentLoop 中剥离；生产异步 Tool
pipeline 仍通过一个宽 `host` 参数读取权限、恢复状态、事务、executor、guardrail、
result projection、handoff、hooks 和 snapshot。本阶段把这些依赖收敛成四组显式能力：

```text
AgentRunner + RunContext
  -> tool_context_from_legacy_host（唯一 host 翻译边界）
  -> ToolExecutionContext
       |- ToolPolicyContext
       |- ToolLifecycleContext
       `- ToolProjectionContext
  -> ProductionToolRuntime async pipeline
```

这里的目标不是复制 TypeScript 文件布局，而是保持 InfCode/InfCodeX 的 ownership 原则：
生产执行器只接收本次 run 所需的状态与端口，SessionRuntime 负责持久 checkpoint，旧
AgentLoop 只存在于组合层 adapter 和同步兼容路径。

### 主要代码变化

- `runtime/core/tool_context.py`：定义 run-scoped policy 状态、lifecycle callbacks、
  projection callbacks 和统一 ToolExecutionContext；batch identity 与 observability 不再
  存在 AgentLoop 模块/实例字段中。
- `runtime/adapters/tool.py`：集中把旧 host 投影成 focused context，并绑定
  SessionRuntime checkpoint、事务、guardrail、executor、result hooks、handoff、snapshot
  与 trace；活跃 RunContext 缺少 Session checkpoint 时 fail closed。
- `runtime/tool_runtime/policy.py`：准入、Agent allowlist、doom-loop、strict progress、
  并发判断和批次观测全部只消费 ToolPolicyContext。
- `runtime/tool_runtime/result_projection.py`：Tool result 的 Session settle、attachment、
  trace、stall 与 post-result hooks 只消费 ToolProjectionContext。
- `runtime/tool_runtime/pipeline.py`：生产 `execute_batch_async` 与 `dispatch_async` 只消费
  ToolExecutionContext；事务、取消收敛、读写调度、checkpoint、result projection、
  handoff 和 step snapshot 不再读取 host。
- `runtime/runner.py`：在首个真实工具批次惰性构造且复用一个 run context；stream tool
  handler 和普通 batch 使用相同 owner。
- `runtime/loop.py`：旧直接调用入口成为薄 adapter facade；同步 pipeline 保持兼容。

### TDD 暴露并修复的问题

1. 三个旧测试会直接调用 `_consume_dispatched_tools()` 或
   `_dispatch_tool_calls_async()`，最初绕过 adapter，把 AgentLoop 传给 focused interface。
   修复是让 legacy facade 显式构造 Projection/Execution context，而不是让核心重新接受
   两种参数形状。
2. Tool context 在 run 开始时快照 active agent；handoff 后 host 已切换，但下一批 policy
   仍可能使用旧 Agent 名称。新增红测后，adapter 的 transition capability 会同步刷新
   context policy identity，确保下一批 allowlist 来自新 Agent。
3. 旧 architecture gate 限制 facade 行数。适配逻辑最终保持在 adapter，Loop facade 仍是
   薄转发，没有为了兼容把业务逻辑搬回 Loop。

### 源码证据与边界

- `ProductionToolPolicy`、`ProductionToolResultProjector.consume()`、
  `ProductionToolRuntime.execute_batch_async()` 和 `dispatch_async()` 的直接 `host.` 访问均为
  0。
- 同步 `execute_batch_sync()` 仍有 22 处、16 类 host 访问；当前唯一生产风格异步 Runner
  不走该路径，但旧直接调用 API 尚未达到删除门槛。
- adapter 仍知道 AgentLoop 私有接口，这是 strangler migration 的明确边界，不能把本项
  描述成整个 Agent Core 已经没有 legacy host。
- Model request、run lifecycle/hooks/snapshot 的部分 Runner 编排以及 child/background
  Session ownership 尚未完成 focused-port 迁移。

### 验证证据

- focused context、policy、projection、checkpoint、direct compatibility 与 handoff 测试均
  经历红—绿验证。
- 全量：`1589 passed in 92.48s`。
- Model/Tool/architecture 专项：`67 passed in 5.01s`。
- Ruff、compileall、6 个关键模块 import smoke、`git diff --check`：通过。
- 离线并发 smoke：6 个任务、顺序保持、peak concurrency=3、speedup=2.93x。
- 未调用付费 Provider、未运行 SWE-bench，项目仍未配置 mypy/pyright，因此不报告外部
  模型效果或静态类型通过。

### 下一步

1. 建立 focused ModelRunContext，迁移 client/capabilities/stream retirement 与请求状态。
2. 把 Runner 的 Lifecycle/Hook/Snapshot 私有访问迁成显式服务端口。
3. 让 Task Tool 创建 parent-linked 原生 child Session，再统一 Background Session 调度。
4. 审计同步 Tool compatibility 的真实消费者，清零后删除 host-shaped pipeline。
5. 最后删除 FileSessionRepository/RunState 等已无生产消费者的兼容层。

## A237：Session-first Runtime 最终生产边界收口

### 这次“一次性完成”的准确含义

本轮连续完成 A236 后列出的四个生产 P0/P1：focused Model、host-free Runner
turn、focused Lifecycle、原生 child/background Session，以及生产服务图中的旧 Session
owner 清理。“完成”只适用于第二阶段提示词的统一执行内核与 Session ownership；不把
同步兼容 API、全局工具副作用注册、IDE 生态或 SWE-bench 分数算作已经对齐。

### 最终生产调用链

```text
CLI / HTTP / SDK / evaluation / task / background / workflow
  -> composition root
  -> AgentLoop compatibility shell
  -> AgentRunner.run
  -> SessionRuntime.open -> RunContext(Session transcript)
  -> RunnerExecutionContext
       -> ContextExecutionContext -> ProductionContextManager
       -> ModelExecutionContext   -> ProductionTurnModelRuntime/Gateway
       -> ToolExecutionContext    -> ProductionToolRuntime
       -> LifecycleExecutionContext -> ProductionRunLifecycle
  -> SessionRuntime.checkpoint/finalize
```

Main、foreground child、background child 和 workflow child 没有独立的 LLM/tool loop。
child 的 worktree、scope、cancel 和 result packaging 仍由 `run_subagent()` 管理，但执行
进入相同 Runner；这与 InfCodeX 的 shared coding substrate 和 infcode-dev 的 Task Tool
创建 child Session 思路组合一致。

### 主要源码变化

- `runtime/core/model_context.py`、`adapters/model.py`：模型能力、tool schema、budget、
  Gateway、stream fallback、outcome projection、cancel retirement 成为显式动态能力；
  handoff 后不会冻结旧模型 capability。
- `runtime/core/runner_context.py`、`adapters/runner.py`：turn control、message projection、
  snapshots、hooks、input、guardrail、transition、verification 和 lifecycle 形成命名端口；
  `_run_turns()` 不再接收 AgentLoop。
- `runtime/core/lifecycle_context.py`、`adapters/lifecycle.py`：run ephemeral state 独立；
  reset、admission、runtime restore、terminal status/evidence 仍由
  `ProductionRunLifecycle` 决策，adapter 只投影宿主状态和副作用。
- `runtime/subagent.py`：child 在进入 Runner 前绑定明确 parent Session ID；恢复优先读取
  native Session，旧 state messages 只用于首次迁移 bootstrap；Runner 接管后 task state
  删除 transcript，只保存 iterations、tokens、cost、verification、scope 和 result。
- `runtime/child_result.py`：iterations 从 task summary fact 读取，不要求复制 transcript。
- `runtime/core/contracts.py`、`runtime/services.py`：RuntimeServices 删除 `sessions` 字段和
  FileSessionRepository 构造；SessionRuntime 成为唯一生产 transcript owner。
- `runtime/session_repository.py`、`runtime/core/state.py`：明确标记为非生产兼容 API，保留
  直接导入，避免无迁移窗口破坏外部调用者。

### TDD 发现并修复的真实问题

1. Model context 若在零轮次 Runner 入口立即构造，会要求极简 host 提供根本不会使用的
   模型能力；改为首个模型调用惰性构造。
2. model async path 的参数 `context` 被 `copy_context()` 局部变量遮蔽，聚焦 Loop 回归
   立即复现并修复。
3. child 过去虽复用 Runner，却没有把 `parent_session_id` 写入 Agent 的 RunRequest，
   SessionStore 因此无法形成真实 parent link。
4. 移除 task-state transcript 后，route iterations 消失；改为独立调度事实，而不是恢复
   transcript 副本。
5. Provider error 的 child 后处理曾只修改 task-state message，native Session 仍保留旧
   end state；现在后处理通过 SessionStore 投影回原生 Session，两者不再分叉。

### 源码证据

- `AgentRunner._run_turns()`：`host.` 0，`host` token 0。
- `ProductionTurnModelRuntime`：`host.` 0，`host` token 0。
- `ProductionRunLifecycle`：`host.` 0，`host` token 0。
- `ProductionToolPolicy`、`ProductionToolResultProjector`：`host.` 0。
- `RuntimeServices`：`sessions` field 0；生产 `services.sessions` consumer 0。
- child 的两个 Provider execution call site 均为 `agent.runner.run(...)`；AgentManager 只
  调度 `run_subagent()`，不拥有 Provider loop。

### 验证证据

- 最终全量复验：`1605 passed in 102.39s`。
- Runtime/Model/Tool/Session/Core/architecture 专项：`152 passed in 5.67s`。
- 产品入口组合（SDK/HTTP/CLI/evaluation/release）：`171 passed in 10.42s`。
- child/manager/session 组合：`100 passed in 8.15s`。
- Ruff、compileall、6 模块 import smoke、`git diff --check`：通过。
- 最终离线并发复验：6 tasks、order preserved、peak concurrency=3、speedup=2.81x。
- 未调用付费 Provider、未运行 SWE-bench，因此不声称模型效果或榜单分数等价。

### 仍保留但不阻塞本轮验收的边界

- 同步 Tool pipeline 仍是 host-shaped legacy API；生产 async Runner 不走该路径。
- `FileSessionRepository`/`RunState` 保留直接导入兼容，但不在 RuntimeServices 或生产
  composition 中。
- 全局 `TOOL_SPECS`/`TOOL_HANDLERS` 和副作用 import 尚未迁成 application-scoped
  registry；原提示词明确要求增量设计而非本轮强制重写。
- input/guardrail/transition/verifier 的实现本身仍是 host-shaped，生产 Runner 通过命名
  adapter port 使用；如果继续做维护性拆分，它们是后续切口，但已不构成第二执行内核。
- 项目仍无 mypy/pyright 配置；终端 UI、MCP 公网互操作和 SWE-bench 是独立产品/外部
  证据任务。

## A238：Native Runtime 去 Host 化 + Child Session 单一真源

### 结论先行

第三阶段规定的七个增量 Phase 已完成，但结论必须分成两层：

- **Native Runtime 成立**：`AgentRunner.run(RunRequest, options=RunOptions)` 可以完全不
  实例化 `AgentLoop`，测试真实经过 Mock Model → Tool → Model → Final，并由
  `SessionRuntime` 打开、checkpoint、终结同一个 `RunContext`。
- **Legacy coding host 尚未消失**：CLI/HTTP/SDK 当前仍构造 `AgentLoop`，再在入口处
  投影 `RunRequest` 和 focused services。它已经不拥有第二套 turn loop，但仍拥有大量
  planning、hook、permission、coding observation 与兼容状态。因此 Q2 的准确答案是
  “尚未成为纯 compatibility facade”，不能写成完全去 Host。

Native 路径不再在模块顶层导入 legacy adapter；只有显式 `_run_legacy()` 兼容入口使用
局部导入。Native 运行异常和取消也会 exactly-once finalize Session，而不是留下 running
状态。

### 三条实际执行链

```text
Native embedding/test
  -> RunRequest + RunOptions
  -> AgentRunner._run_request
  -> SessionRuntime.open -> RunContext
  -> shared Context/Model/Tool/SessionProcessor turn loop
  -> SessionRuntime.finalize

CLI / HTTP / SDK / Evaluation (legacy product entry)
  -> AgentLoop compatibility boundary
  -> run_request_from_legacy_host (one-way)
  -> AgentRunner.run(RunRequest, RunOptions)
  -> same shared turn loop

Task / SubAgent
  -> TaskRecord(worktree/scope/verification/application status)
  -> AgentDefinition + child RunRequest(parent_session_id)
  -> agent.run -> same AgentRunner / SessionRuntime / SessionProcessor
  -> Child Session transcript + Task result projection
```

### RunnerExecutionContext 45 个 callback 的归属审计

修改前共有 47 个字段，其中 45 个是 callback。修改后为 10 个字段、0 个 Callable
annotation：`session_id`、`runtime_state` 加 8 个具名 owner。下面列出的 45 项是完整
能力清单，不是用新的平面 callback bag 改名掩盖旧结构。

| 原 callback/能力 | 数量 | 修改前最终实现/状态 owner | 修改后 owner |
|---|---:|---|---|
| `context`, `model`, `tools` | 3 | AgentLoop adapter factories | `execution` focused context factory；具体执行仍在 RuntimeServices |
| `initialize`, `finalize` | 2 | AgentLoop lifecycle methods | `lifecycle` / `ProductionRunLifecycle` |
| `run_input_guardrails`, `has_output_guardrail`, `run_output_guardrail`, `prepare_user_images`, `prepare_user_documents`, `resolve_structured_output`, `return_from_as_tool`, `terminal_content`, `verify_completion` | 9 | AgentLoop policy/transition methods | `policy` owner，转发必需 Guardrail/Input/Transition/Verifier ports |
| `generate`, `replan` | 2 | `_maybe_generate_plan`, `_maybe_replan` | `LegacyPlanningRuntime`；仍是 legacy coding owner，列为后续债务 |
| `has_queued_followup`, `drain_background_messages`, `has_agent_call_stack`, `notify_agent_switched`, `persist_runtime_state`, `stop_hook_reason` | 6 | AgentLoop mutable control state | `control` owner；属于 facade 尚未纯化的主要阻塞项 |
| `on_turn_start`, `on_pre_send`, `on_turn_end`, `trace` | 4 | AgentLoop hooks/tracer | `hooks` event/hook sink |
| `persist_compaction_exhaustion`, `bind_assistant_context`, `bind_user_contexts`, `new_message_part`, `publish_event`, `materialize_llm_result`, `reconcile_llm_result`, `bind_active_processor`, `build_api_messages`, `apply_usage_cost`, `observe_llm_result`, `compact_messages`, `stamp_auto_compaction`, `inject_api_diagnostic` | 14 | AgentLoop message/context helpers | `LegacyMessageRuntime` + `SessionProcessor`；Run usage 同时迁入 `RunContext` |
| `capture`, `await_start`, `retire`, `capture_async`, `record_patch` | 5 | AgentLoop snapshot/change helpers | `LegacySnapshotRuntime` / coding observer |

`RunnerExecutionContext` 当前已不是 callback bag，但仍是 legacy composition 产生的一组
run-scoped service owners。Native 单测可注入完全不依赖 AgentLoop 的 owner；生产 Main
仍从 legacy facade 构造其中 planning/control/message/snapshot 的部分实现。这正是没有把
Q2 写成“是”的原因。

### Tool / Context / Model 第一批去 Host 化

- `ProductionContextManager` 和 `ProductionTurnModelRuntime` 只消费 focused context，
  直接 `host._xxx` 为 0。
- 生产异步 `ProductionToolRuntime.execute_batch_async/dispatch_async` 只消费
  `ToolExecutionContext`。代码索引、LSP、patch risk、snapshot、verification 后效应由
  `LegacyCodingToolObserver` 注入，generic async pipeline 不直接 import 这些实现。
- 同步 `execute_batch_sync()` 仍是明确的 legacy API，保留 16 处 `host._xxx`。本阶段没有
  为了数字好看删除仍有消费者的同步兼容入口。

### Child Session 与 TaskRecord ownership

新 Child TaskRecord 不再创建 `messages`。恢复旧任务时，只有在 SessionStore 尚无原生
Session 时才读取一次 legacy messages 作为 bootstrap；一旦 Agent 被构造并进入 native
run，无论是首次还是恢复，Session 都成为权威，TaskRecord 保存前会删除：

```text
messages
tokens
cost
cost_known
iterations
```

usage/cost/iterations 仍可出现在一次调用返回的 `child_result` 中，这是给父 Task/界面消费
的终态投影，不参与下一次 transcript 或运行状态重建。`TaskStatus` 已成为独立枚举，描述
queued/running/cancel/application/verification 生命周期；`SessionStatus` 只描述 conversation
run。二者不再被解释为同一种状态。

| State | 修改前 owner | 修改后 owner | Lifetime |
|---|---|---|---|
| transcript/message parts | AgentLoop + child state JSON + Session | Session / SessionProcessor | durable Session |
| session status | child state string + Session | SessionStatus | each run, resumable Session |
| usage | messages + child top-level tokens + Session | RunContext aggregate → Session.usage；child_result 仅投影 | run + durable Session |
| turn/retry/compaction count | AgentLoop fields | RunContext（部分 legacy 统计仍投影到 facade） | one run |
| model selection | AgentLoop/child route state | RunRequest declaration + focused Model context | request/run |
| permissions/tool exposure | AgentLoop mutable config | AgentDefinition/RunProfile + ToolPolicyContext | request/run |
| cancellation | entry callbacks/child event | RunOptions/RunContext + Tool/Model cancellation ports | one run |
| snapshot/patch | AgentLoop callbacks | SnapshotRuntime + coding observer + Session parts | step/session |
| plan/replan | AgentLoop | LegacyPlanningRuntime（待成为原生 coding service） | run/session |
| handoff/structured output | AgentLoop | TransitionRuntime + SessionProcessor | run/session |
| provider runtime | AgentLoop client/model fields | focused ModelExecutionContext/Gateway | run/turn |
| tool batch/transaction | AgentLoop/ToolExecutor | ToolExecutionContext/ProductionToolRuntime | one batch |
| memory | AgentLoop MemoryManager | Runtime MemoryService；legacy host 提供具体 manager | workspace/session |
| parent session | child state + ad-hoc field | SessionIdentity；TaskRecord 只保留关联 foreign key | durable identity |
| child task status | 与 Session status 混用的 string | TaskStatus | task/application |
| worktree/scope/conflict | subagent state | TaskRecord/WorktreeManager（保持不变） | task |
| verification | subagent state + post-processing | TaskRecord contract/result + coding verifier | task/result |

### 量化结果

| 指标 | 修改前 | 修改后 | 解释 |
|---|---:|---:|---|
| `runtime/loop.py` LOC | 3773 | 3826 | 增加 native facade/sync compatibility；没有为了数字拆空文件 |
| AgentLoop `__init__` attrs | 95 | 95 | 真实 owner 尚未全部迁完 |
| AgentLoop methods | 160 | 162 | 新增 native facade/adapter helper |
| `runtime/runner.py` LOC | 971 | 1005 | Native API、终态错误收口与兼容入口共存 |
| RunnerExecutionContext fields | 47 | 10 | 8 个 cohesive owners + identity/state |
| RunnerExecutionContext callbacks | 45 | 0 | 不再暴露平面 Callable 字段 |
| adapters `host._xxx` | 34 | 28 | 剩余集中于 lifecycle/context/tool legacy adapters |
| Context runtime `host._xxx` | 0 | 0 | 已 focused |
| Model/service group `host._xxx` | 16 | 16 | 数字来自 Memory/Event legacy implementations；`ProductionTurnModelRuntime` 自身为 0 |
| Tool runtime `host._xxx` | 16 | 16 | 全部在同步 compatibility；生产 async 路径为 0 |
| `subagent.py` LOC | 2234（阶段基线） | 2254 | 新增 Session-only迁移、TaskStatus 与验证证据修复 |
| 新 Child TaskRecord initial fields | 21 | 20 | 删除 `messages`；native terminal 另删除顶层 usage/iterations |

Host 统计口径为源码正则 `host._[A-Za-z0-9_]+` 的出现次数，不把 `getattr(host,
"_name")` 算作直接私有属性语法；因此 adapter 数量反映静态直接访问，不等于依赖已经
消失。

### Circular dependency / SCC

历史 A234 使用的绝对 import 扫描基线为 5 个多模块 SCC、最大 20 个模块。本轮按同一
兼容口径复扫为 4 个 SCC，最大组 21 个模块；另外三组大小为 4、3、2。剩余组是：

1. runtime/composition/subagent/context 与 tools/project_creation/state 组成的 21 模块组；
2. Provider base/capabilities/extensions/registry 的 4 模块组；
3. CLI/fullscreen/terminal_input 的 3 模块组；
4. SWE guardrail/models 的 2 模块组。

为了避免旧扫描器漏报，本轮还增加一次解析 relative import 和 facade re-export 的严格
审计：5 个 SCC，最大组 58 个模块。这个数字不能和旧 5/20 直接比较，但揭示了
`nz_coder.loop`、`nz_coder.subagent`、runtime composition 与工具副作用注册构成的真实
大环，是后续 P1，而不是本轮伪造“循环依赖已清零”。

### InfCodeX / infcode-dev / NZ-Coder 三方能力矩阵

矩阵基于重新扫描 `references/InfCodeX/packages/agent/src/primitives/*`、
`packages/coding/src/agent-runtime/*`，以及 `infcode-dev/.../agent/agent.ts`、
`session/{prompt,processor,llm,compaction,message-v2}.ts`、`tool/task.ts`、permission、
snapshot 源码。

| Capability | InfCodeX | infcode-dev/OpenCode | nzcoder | 状态 | 下一步 |
|---|---|---|---|---|---|
| Agent primitive | immutable agent/tools/handoffs/middleware | config-backed Agent namespace | AgentDefinition/Graph | Mostly aligned | 将 coding preset 彻底移出 AgentLoop |
| Runner | generic Runner + coding substrate | SessionPrompt/Processor 驱动 | native AgentRunner + shared turn loop | Mostly aligned | 移除 production execution-context legacy factory |
| Session | generic Session + coding runtime state | Session/Message DB 为核心 | Session/Store/Runtime/RunContext | Aligned | 增加 schema migration/versioning |
| Message/Part | transcript + event model | 强类型 MessageV2 parts | message schema + SessionProcessor | Mostly aligned | 减少 dict 与 legacy metadata |
| Streaming | Runner stream events | processor consumes provider stream | gateway + processor + terminal callbacks | Mostly aligned | 统一 typed event envelope |
| Provider | LLM package + per-turn resolution | 多 Provider/transform/auth | provider registry/gateway/capabilities | Mostly aligned | 公网兼容矩阵与更多原生 wire adapters |
| Tool Runtime | runner loop + coding execution context | Tool registry + processor | async ToolRuntime/context/policy/projector | Mostly aligned | 删除 sync host-shaped pipeline |
| Permission | invariant/capability gate | rule engine + ask/reply bus | PermissionManager + guardrail/tool policy | Mostly aligned | application-scoped permission service |
| Context Budget | coding context-budget | model-aware budget | focused ContextManager | Mostly aligned | 统一每模型精确 token estimator |
| Compaction | middleware orchestration/pressure | SessionCompaction + prune/replay | compaction service + SessionProcessor | Mostly aligned | 将 message compaction wrapper 去 legacy host |
| Snapshot | middleware session snapshot | Snapshot service/parts | SnapshotRuntime + workspace snapshot | Mostly aligned | native snapshot owner 替代 legacy wrapper |
| Recovery | bounded revise/edit recovery | retry/compaction/error parts | recovery/stall/diagnostics | Mostly aligned | 将恢复策略变成 composable middleware |
| Main Agent | coding preset | primary SessionPrompt | legacy facade → native Runner | Mostly aligned | 让 product composition 不构造 AgentLoop |
| Child Agent | shared coding substrate | TaskTool creates child Session | same Runner + parent-linked Child Session | Aligned | TaskRecord typed schema |
| Background Agent | managed task layer | background/agent manager extensions | AgentManager delegates child | Mostly aligned | 本轮按要求未全面重写 |
| Agent messaging | extension/event channels | task/session messages | message_parent/send_message + manager | Mostly aligned | durable mailbox/ack semantics |
| Handoff | first-class continuation/as-tool | agent/task transitions | AgentHandoff/TransitionRuntime | Mostly aligned | native transition state owner |
| Workflow | coding workflows/invariants | plugin/task orchestration | Workflow runtime + capsules | Different by design | 保留 Python worktree/verification 优势 |
| Guardrail | guardrails + invariant session | permission/tool/session checks | GuardrailRuntime + verification | Mostly aligned | 去除 host-shaped concrete guardrail |
| Middleware | first-class declarations/composition | hooks/plugins around Session | hooks + observers + fixed services | Partial | P0：typed ordered middleware chain |
| Verification | judges/invariants/sidecar | diagnostics/tests/tool results | layered verifier/sidecar/quality gate | Different by design | 用真实 SWE trace 校准策略 |
| Memory | coding middleware/integration | project/session instructions | working/session/long-term memory | Mostly aligned | 去 host 化 concrete MemoryService |
| MCP | tool fallback/integration | mature MCP clients/config | stdio/SSE/HTTP/OAuth client | Mostly aligned | 公网 server interoperability suite |
| Skills | coding skills | agent/command/config skills | bundled/workspace/user skills | Mostly aligned | precedence/security conformance |
| LSP | coding tools/integrations | LSP tools | native stdio JSON-RPC LSP | Mostly aligned | 多语言真实 server matrix |
| Repo Intelligence | middleware/routing | indexing/search extensions | Repo Map/ranking/LSP/search | Mostly aligned | Tree-sitter/增量持久索引 |
| Code Index | middleware/index integration | kilo indexing | Python/multi-language structural cache | Partial | application-scoped incremental index |
| Tool Search | exposure planner/resolution | dynamic registry | optional tools/search/allowlist | Partial | 语义 tool discovery 与 bounded exposure |
| Dynamic Tool Exposure | planner/capability clamp | per-agent permission/tools | Agent allowlist + dynamic scopes | Mostly aligned | 移除全局副作用 registry |
| SDK | 核心强项，typed Runner API | HTTP/SDK clients | Python AgentClient/run_agent | Partial | 直接消费 Native Runner，不构造 facade |
| HTTP Runtime | daemon/worker/client | HTTP+SSE server | native HTTP service/session API | Mostly aligned | 长时并发与断线恢复 soak |
| Tracing | spans/events/cost | bus/events/message parts | TraceRecorder/session events | Mostly aligned | OpenTelemetry/统一 trace schema |
| Metrics | performance/cost tracker | usage/status endpoints | usage/session stats/eval summaries | Partial | 稳定 Prometheus/OTel metrics contract |
| Evaluation | benchmark harness/contracts | project tests/evals | SWE strict runner/trace budget | Mostly aligned | Verified 500 严格 pass@1 外部证据 |

### Q1—Q6 明确回答

**Q1：可以。** `await runner.run(request, options=...)` 不实例化 `AgentLoop` 已由完整
Model→Tool→Model→Final 测试证明；Session usage 为 13 input / 4 output，transcript 顺序
为 user/assistant/tool/assistant。

**Q2：还不是纯 facade。** 它已不拥有第二 turn loop，Main 也先构造 RunRequest 再进入
Native Runner；但 95 个 init attrs 未下降，planning/control/message/snapshot 的 legacy
owner、具体 Memory/Event/Lifecycle adapter、CLI 兼容 API 仍阻止其成为可删除外壳。

**Q3：基础 Runtime 语义相同。** Main/SubAgent 使用同一个 AgentRunner、SessionRuntime、
SessionProcessor、ContextManager、ModelRuntime/Gateway 和异步 ToolRuntime。差异只来自
AgentDefinition、RunProfile、workspace/worktree、model route、permissions 和工具暴露。

**Q4：是（native task）。** transcript/resume/message parts/usage 的 durable source 只有
Child Session；旧 `state.messages` 只允许在没有 native Session 时 bootstrap 一次。

**Q5：是，按运行语义划分。** TaskRecord 剩下 scope/worktree/conflict/verification、
application `TaskStatus`、route declaration、display/trace/task metadata 和终态
`child_result` 投影；不再用 messages/usage/iterations 重建执行。`parent_session_id` 是
Session foreign key，不是 transcript 副本。

**Q6：最大 10 个差距（工程优先级排序）。**

1. Product composition 仍构造 AgentLoop，95 个 mutable attrs 尚未真正迁完。
2. 缺少 InfCodeX 风格可组合、可排序、带类型的 middleware pipeline。
3. 同步 Tool compatibility 仍直接访问 legacy host，全局工具 registry 仍是进程级状态。
4. Message/Part 仍大量使用动态 dict，缺少 OpenCode MessageV2 级 schema/version migration。
5. 大型 import SCC 未解决，facade re-export 与副作用 import 放大耦合。
6. Provider/MCP/LSP 的“存在”测试多于跨真实服务的互操作/长时稳定证据。
7. Code Index 缺少 Tree-sitter 级增量、多语言语义与可替换持久后端。
8. Background/Workflow 尚未统一为 typed durable task protocol（本轮按要求停止）。
9. SDK/HTTP 仍通过 legacy product composition，尚未成为 Native Runner 的最薄客户端。
10. 没有新的 SWE-bench Verified 500 严格 pass@1 结果，架构对齐不能推导成绩等价。

### 验证与未声称内容

- 新增行为均先观察失败再修复：Native API、Main facade、异常终态、child parent identity、
  Session restart resume、首次 TaskRecord messages、usage 双写、Session transcript verification。
- 最终全量：`1613 passed`；Ruff、compileall、关键 import smoke、`git diff --check` 通过。
- 没有调用付费 Provider，没有运行 SWE-bench，没有运行公网 MCP/LSP/HTTP soak；因此不
  声称实时模型效果、榜单分数或终端产品体验与任一参考项目相同。
# A239 — Runtime 架构收口与能力追赶基线（2026-08-11）

- 新增单一 `MiddlewarePipeline`，明确 before 正序、after/error 逆序与原始异常权威语义，并接入 Runner 的 run/model/tool batch。
- 将 `RuntimeEvent` 变成 host-free contract，生产 sink 复用 `SessionEventBus`；Legacy Main 抑制重复事件。
- SDK 新增直接 `AgentRunner.run_result()` 路径以及 parent-linked child/resume；公开 Agent/Runner/Session/Request/Result/Tool contract。
- Background/Workflow 继续复用 `run_subagent -> AgentRunner`，新增标准 child lifecycle 事件和静态禁止 model/tool loop 的 guard。
- 20/60/120 工具 benchmark 显示 schema 约 1,036/3,112/6,275 coarse tokens，lexical recall@8 为 100%；由于没有真实模型成功率证据，本阶段不启用动态过滤。
- 新的三方矩阵显示下一项高价值差距是大型仓库 Repo Intelligence（增量、多语言 call graph、semantic index），但必须先由 SWE trace benchmark 决定具体实现。
# A240 — Capability Parity: Tool/Repo/Skills（2026-08-11）

- 新三方审计不再把文件名或工具数量当作能力证据，使用 Aligned/Mostly aligned/Partial/Missing/Different by design，并记录 76 项能力。
- Tool Intelligence 新增 immutable catalog、ranked search、6k schema budget、resident/deferred、run-owned unlock 和 `tool_search`；120/200 tools 的模型可见 schema proxy 从 8,259/13,859 tokens 降到 169。
- Repo Intelligence 保留 SQLite code index，新增 Python/TS/JS/Go/Rust 持久 module/import graph、incremental fingerprints、dependencies/dependents、SCC cycles、changed scope 与 `repo_context`。明确不把 lexical/structural 能力冒充 semantic search。
- Skill `model` 现在真正解析保存，provenance/resource base 可审计，`allowed_tools` 从注释升级为 ToolRuntime 执行时交集约束；ContextVar 保证并发 Session 隔离。
- 默认 Public SDK 仍可能构造 AgentLoop，被如实列为 P0；本轮没有以 Capability 名义重新打开全局 Runtime 重构。

## A241 — Unified tool-result budget and governed automatic memory (2026-08-11)

- Source-level audit confirmed that provider-visible tool results had a focused append boundary but no single token policy. Added `ToolResultBudget` and `ToolResultProjector` there, preserving bounded head/tail evidence and a durable full-output reference.
- Automatic memory extraction previously called `MemoryManager.save()` directly. It now creates durable proposals with Session/message provenance, confidence, reason, fingerprint, risk and review status, then applies only after policy approval.
- Deterministic benchmark: tool output 35,013→1,483 visible tokens with both sentinels retained; a poisoned memory changed from one direct save to zero and entered review.
- Revalidated the SDK default chain. Native Runner existence does not make the product SDK native while production services still require host-shaped state; no fake factory-level alignment was introduced.

## A242 — MCP prompt/resource Agent-facing discovery (2026-08-11)

- MCP transport/OAuth/trust/lifecycle were already mature; the missing behavior was model-side discovery of cached prompts and resources.
- Added a ContextVar-bound active MCP runtime and one bounded `mcp_catalog` operation tool supporting search, exact prompt fetch and exact resource read.
- The catalog exposes no commands, environment variables, credentials or OAuth state, and concurrent runs cannot see each other's runtime binding.
- Native SDK default migration remains a separate P0 because its production services are still host-shaped; this change does not pretend otherwise.

## A243 — Native SDK prerequisites: focused Memory and Verifier contexts (2026-08-11)

- Replaced broad-host inputs in `ProductionMemoryService` with a focused memory manager/session/model/tracer/lineage/recall-cache context.
- Replaced broad-host input in `ProductionCompletionVerifier` with an override/review callback context.
- AgentLoop compatibility is isolated in `runtime/adapters/memory.py` and `runtime/adapters/verification.py`; production services contain no `host.` access.
- Public SDK default is not yet marked Native: the remaining host-shaped services and coding observer are still listed explicitly.

## A244 — Public SDK defaults to Native Runner (2026-08-11)

- `AgentClient()` and `run_agent()` now construct `NativeSDKRunner`, not `AgentLoop`; importing the SDK leaves `nz_coder.runtime.loop` unloaded.
- Added a run-scoped native composition for model selection/client ownership, durable Session state, context budget, tool registry dispatch, permissions, transaction/change tracking, tool-result projection, guardrails and structured-output repair.
- `agent_factory=` remains an explicit legacy compatibility escape hatch; `runner=` remains the dependency-injection path.
- Added an offline end-to-end acceptance test whose fake Provider requests `list_directory` on turn one and consumes its real tool result on turn two.
- This closes the hidden-default P0. CLI-only planning/snapshot/background integrations remain product-depth work and are not mislabeled as SDK core blockers.

## A245 — Terminal Product Phase 6（2026-08-11）

- 新增 `nz-coder run`：stdin/positional、fresh/continue/resume/no-session、
  provider/model/effort/permission/max-turns、file/attach，以及无 Rich 污染的
  text/JSON/JSONL 和稳定 0/1/2/3/4 exit code。
- Headless 与 Python SDK 均进入 `AgentClient -> NativeSDKRunner -> AgentRunner`；
  离线测试真实覆盖 Model→Tool→Model→Final，并证明不构造 AgentLoop。
- `RunOptions.on_event` 只投影现有 RuntimeEvent；`EphemeralSessionStore` 支持
  无持久化运行，没有建立第二事件或 Session 协议。
- Ctrl+V 图片覆盖 Linux/macOS/Windows/WSL，图片私有落盘后复用 FilePart；
  路径 paste、`@file`、`/attach`、headless file flags 共用提交边界。
- `!command` 经 PermissionManager/ToolExecutor/bash 执行且不注入 transcript；
  bash/zsh/fish completion 全部离线生成。
- 新建 93 项 Terminal Product 三方矩阵。daemon/attach/reconnect、memory inbox、
  agent picker、custom commands、persistent PTY 按阶段边界延期。

## A246 — Product Runtime Convergence Phase 7（2026-08-11）

- 重新核实真实调用链：旧 Interactive/HTTP 使用完整 AgentLoop，而 SDK/Headless
  的 `native_sdk.py` 有 Memory、Verifier、Planning、Snapshot、Input no-op，确实是
  Product Runtime Split，不是报告误差。
- 将成熟能力所有者提升为 `ProductRunEnvironment`；`AgentLoop` 只剩显式兼容子类。
  默认 SDK、Headless、HTTP、Interactive 现在统一进入
  `RunRequest → AgentClient → NativeSDKRunner → ProductRunEnvironment → AgentRunner`。
- 删除 Native 重复实现，直接复用 Production model/tool/context/memory/verifier/
  lifecycle/guardrail/input/transition service、ToolExposure middleware 以及 Host 中
  MCP、Skill、Memory、Background、Workflow、事务和事件资源绑定。
- HTTP 保留 CRUD、SSE、Last-Event-ID、gap repair、snapshot、abort 和交互 broker，
  但不再默认长期持有 AgentLoop；Terminal 保留现有 UI，只增加
  `TerminalSessionController` 并迁移首批显式控制能力。
- 增加四入口能力指纹、无 AgentLoop 架构守卫和 Headless 图片直达视觉 Provider
  测试。SWE-bench/benchmark composition 也改用 Product environment。
- Memory 必须表述为 Backend Aligned / Product Partial；Extension 是 Registry Mostly
  aligned / Lifecycle UX Partial；Session 与 TUI 都不是整体 Missing。
- 本轮不实现 PTY、语义搜索、Web Search、Plugin Marketplace 或 Markdown Commands。
  Daemon/Attach 仅形成带 owner fence、0600 token、重连/cursor resync 的下一阶段计划。
## A247 — Core Coding Capability Sprint（2026-08-11）

This phase intentionally stopped terminal-product expansion and closed three core
capability clusters after a source-level audit of NZ-Coder, InfCodeX repository
intelligence, and OpenCode/Kilo indexing.

## What changed

- Repository Intelligence V3 now persists Python AST call edges alongside symbols and
  references. `symbol_context`, `callers`, `callees`, `process_context`, module symbol
  capsules, and Changed Scope V2 all consume the same SQLite index. Incremental file
  replacement also removes stale call edges. A workspace-owned background prewarm
  service exposes `cold`, `warming`, `ready`, and `failed` freshness states.
- Impact V2 can consume structural changed symbols, affected callers, and graph-related
  tests instead of relying only on path/diff heuristics.
- Tool exposure now accepts real context pressure (window, used input, output reserve,
  schema ratio). Large catalogs stay visible when capacity is ample and defer rare
  tools only under actual pressure; run-owned unlocks remain compatible.
- Tool result projection now has one aggregate batch ceiling and named evidence
  policies: reads/searches favor heads, shell/tests favor tails, diffs favor both.
  Every truncated full output remains recoverable from an immutable artifact.
- A provider-free A–H benchmark and `AgentTrajectoryMetrics` now measure localization,
  cross-file impact, repository navigation, 20/50/100/200 tools, huge output, 40-turn
  horizon, verification recovery, and child conflict accounting.

## Evidence and honest remaining gap

Initial focused regression passed; final full regression: 1741 tests passed. Local A–H evidence
is stored at `.nz-coder/benchmarks/core-capability-20260811-v2/core-capability-report.json`
with manifest hash `d6edd21f6ec92a3b`; all eight deterministic checks passed.

Score dimensions must not be conflated:

- Feature Coverage: 100/100 for this phase's A–H manifest.
- Implementation Depth: 75/100. Structural Python calls are high-confidence, but rich
  call/reference extraction is not yet equally deep for every supported language.
- Behavioral Effectiveness: 100/100 only on the deterministic local A–H suite. Real
  model/SWE-bench effectiveness remains unknown until separately measured.

Semantic embeddings were not added. InfCodeX/Kilo show useful worker, cache, watcher,
parser, and semantic-search designs, but this phase produced no structural benchmark
failure that justifies a vector-store/provider dependency. This is a deliberate
evidence-based deferral, not a parity claim.

### A247 completion follow-up

The initial A247 implementation was subsequently audited against every prompt item.
The follow-up added lower-confidence multi-language references/calls, ambiguous symbol
capsules, exports/entrypoints/test mapping, reverse process context, structural
localization, a continuously coalescing product-owned watcher, and live ContextManager
pressure wiring. A–H was upgraded from synthetic counters: case C indexes 300 modules,
case F runs the canonical native AgentRunner for 41 model/40 tool rounds, case G executes
a real fail→repair→pass `py_compile` cycle, and case H uses the production background
agent conflict boundary. The first real rerun exposed and fixed relative-path and
same-directory idempotency defects. Final evidence and the strict gap matrix are in
`docs/core-coding-capability-sprint-audit.md`.

## A248 — Terminal Product Parity final productization（2026-08-13）

- Core Runtime remained frozen. Embedded, Headless, SDK, HTTP, and Remote continue
  through the canonical `RunRequest -> ProductRunEnvironment -> AgentRunner` path.
- Remote attachments now reuse the canonical FilePart submission pipeline and are
  revalidated inside the daemon workspace. This work exposed and fixed an orphaned
  message identity that could drop image parts.
- Added inert project/user/bundled Markdown commands with precedence, arguments,
  slash completion, tool-policy narrowing, package data, headless support, and
  daemon-side workspace resolution for Remote.
- Extension lifecycle now delegates skill enable/disable/reload to the real owner;
  MCP/hook/tool-pack operations report actual hot-reload or `restart_required`
  semantics. Memory stale-proposal tests confirmed the existing compare-and-apply
  core, so Memory Core was not rewritten.
- Embedded and Remote use the same idempotent high-frequency tool renderer. Added
  secret-free `config show`, classified doctor output, and explicit Linux/macOS/
  Windows/WSL capability probing; Windows remains an honest pipe tier without ConPTY.
- The first isolated release run caught two issues hidden by the development checkout:
  inherited `PYTHONPATH` made the fresh venv import source, and `/status` imported a
  nonexistent `nz_coder.status`. Both were fixed at their source and covered by tests.
- Final `ProductScenarioSuite` T1–T20 passed 20/20. It includes an isolated wheel,
  installed real PTY, authenticated daemon attach/reconnect, Remote interactions,
  process, custom command, skill, MCP, memory, extension, large output, platform,
  and JSONL evidence. The 105-capability audit and honest remaining gaps are in
  `docs/terminal-product-parity-final-report-2026-08-13.md`.
- Final source regression passed 1986 tests on Linux/Python 3.13 with seven known
  multiprocessing `fork()` deprecation warnings and no failures. Ruff, compileall,
  diff checks, wheel/sdist build, isolated install, real PTY, and daemon smoke also
  passed. These results do not substitute for macOS, Windows, live-provider, or
  SWE-bench evidence.
- The final closure added Embedded/Remote `/agents`, daemon-owned Remote Workflow
  prepare/approval/control, Memory edit/delete and Remote review, and real extension
  owner lifecycle calls. Workflow approval now resolves exactly once so the plan
  fingerprinted for approval is the same object submitted for execution.
- Product stress now includes explicit three-disconnect delayed SSE recovery, exact
  100 KB/1 MB output, 10k files, 1k Sessions, CJK/emoji/control bytes, and large
  input boundaries. The final machine report also records installed CLI startup,
  attach/reconnect, command-discovery latency, and zero orphan processes.
- Strict acceptance replaced the T2/T3 component proxies with real PTY journeys.
  T2 covers `/connect`, masked key input, model discovery, private persistence,
  and activation. T3 executes an SSE model→`write_file`→model turn and verifies
  its file on disk.
- The dead-product audit removed an unused command alias and obsolete SDK factory.
  Full regression proved the injected-controller branch still owns cancellation
  recovery, so it was retained. Live adapters have named consumers and removal
  gates in `docs/legacy-product-code-audit-2026-08-13.md`.

## A249 — Windows + Terminal UX Release Candidate（2026-08-13）

- Agent Core stayed frozen. Platform behavior moved behind immutable shell/path/
  encoding contracts and ProcessService-owned POSIX PTY, Windows ConPTY, and pipe
  backends. Public `bash` remains compatible, but no product execution path relies
  on `shell=True`.
- Windows execution selects PowerShell 7, Windows PowerShell, then cmd. Job Object
  binding and bounded `taskkill /T` cleanup replace single-process kill. pywinpty is
  a Windows-only dependency; `.cmd/.bat/.ps1` LSP/MCP stdio launch is explicit.
- Fullscreen UX gained an actionable empty/no-provider state, LOCAL/REMOTE and
  textual run state, Ctrl+K discovery, attachment chips, responsive bands, semantic
  Agent activity, Normal tool-detail default, categorized errors, and risk-explained
  permissions.
- Custom-command model frontmatter now reaches immutable per-run requests in
  Embedded, Remote, and Headless without mutating global/Session model state.
  Authenticated Remote process write/resize routes were completed.
- W1–W15, U1–U14, and Windows/Linux R1–R12 manifests plus `windows-latest` CI are
  executable release owners. Linux release smoke passed wheel, sdist, isolated
  install, daemon, and real PTY at 394.078 ms cold startup on the final run.
- The final real product benchmark passed T1–T20 at 20/20 with 459.769 ms
  startup, 0 duplicate events, and 0 orphan processes. The final full repository
  regression passed 2019 tests with 10 native-Windows-only skips.
- Final performance evidence records 394.078 ms installed cold startup,
  336.190 ms warmed median startup, and 12,016-character Markdown rendering at
  99.898 ms median / 103.771 ms p95 with a 2.783 MiB traced allocation peak.
- Re-audit found and closed one weak evidence edge: Windows CI now installs and
  discovers basedpyright, TypeScript language server, and gopls, imports each
  default Tree-sitter wheel, starts the real FullscreenComposer, and performs an
  actual MCP stdio child/tool round-trip. Linux does not impersonate this native
  evidence; the hosted Windows job remains the release gate.
- Native Windows execution is deliberately recorded as pending CI, not inferred
  from Linux mocks. The full report is
  `docs/windows-terminal-ux-rc-report-2026-08-13.md`.

## A250 — Windows + TUI RC Closure（2026-08-13）

- Re-audited actual process paths instead of trusting the A249 report. Both
  one-shot shell and persistent process output now stay as raw bytes until the
  shared BOM → UTF-8 → configured encoding → system encoding → safe replacement
  decoder. UTF-8/CJK/Japanese/emoji, UTF-16, CP936, and malformed-byte fixtures
  cover the contract.
- Windows Job Objects now set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` before
  binding only the child PID created by ProcessService. Real ctypes HANDLE
  prototypes prevent 64-bit truncation. Pipe and ConPTY backends share the Job;
  direct PID/tree termination remains a bounded fallback.
- Added native WC1–WC5 owners for normal kill, Session cleanup, daemon graceful
  stop, forced parent exit, and npm → node descendants, plus PowerShell 7/5.1
  multilingual output and a real junction escape test. These collect as skips on
  Linux and must pass on `windows-latest`; Linux mocks are not native evidence.
- TUI clipping is terminal-column and conservative-grapheme aware for ASCII,
  CJK, emoji, combining marks, and ZWJ sequences. Header priority is now status,
  location, workspace basename, model/mode, then Session title/short ID.
  Clipboard-cache attachment names are presented as `[clipboard image]`.
- `/help` is Essentials-only with `/help all`; Ctrl+K palette order is suggested,
  recent, common, then searchable all across stable product categories. The
  primary shortcut copy no longer advertises Ctrl+P alone.
- Attached terminals persistently identify `LOCAL DAEMON` versus
  `REMOTE · endpoint`. Attach mode never runs `!command` on the client, and a
  true remote URL rejects client-local files instead of sending meaningless
  paths. Missing pywinpty falls back to pipes with an actionable install hint.
- Acceptance is now executable evidence, not a manifest: W/U/R runners record
  scenario, platform, result, duration, failure, environment, Python, and package
  version; CI uploads JSON artifacts. The fresh Linux T1–T20 run exposed a real
  Remote `Path`-shadowing failure (19/20), which was fixed before rerun. The R
  runner likewise exposed stale nonexistent file/clipboard test owners; both
  manifests were corrected.
- Source comparison retained the useful reference semantics rather than copying
  terminal frameworks: OpenCode's suggested-command overlay and remote footer
  location, InfCodeX's measured-width/process-tree/session ownership, and
  NZ-Coder's existing prompt-toolkit/ProcessService boundaries were composed as
  one Python product. No React/OpenTUI emulator or process-name kill scan was
  introduced.

## A251 — Windows Private State + Runtime Diagnostics Closure（2026-08-13）

- 把 POSIX `chmod` 与 Windows DACL 明确拆成一个中立的
  `nz_coder.private_paths` contract。Windows 使用标准库 `ctypes` 调用 token/SID、
  SDDL、`SetNamedSecurityInfoW` 和 `GetNamedSecurityInfoW`，建立 protected DACL，
  只允许当前用户与 Local System full control；实际 ACL 经过回读验证，失败保留
  honest Tier B，不再把 `0600` 文案当作 Windows 安全证据。
- DACL 接入 daemon root/token/state/lock/log、Provider `.env`、初始化 `.env`、
  Session 根/运行时 JSON、clipboard attachment、prompt history 和 terminal
  preferences。Provider credential 采用 fail-before-write：Windows 临时文件先完成
  owner-private ACL，失败时不写 secret、不替换原 `.env`。
- `platform` 根据 DACL adapter 报告 source capability；Doctor 不读取 secret，直接
  检查 `.nz-coder` 与 `.env` 的真实权限并输出 A/B 建议。`nz-coder init` 只有在
  hardening 成功时才显示 owner-private，否则明确 best-effort 并引导 Doctor。
- Shared process decoder 新增无 BOM UTF-16LE/BE conservative detection，并从
  `GetConsoleOutputCP`、`GetOEMCP`、`GetACP` 获取真实 Windows codec candidates；
  one-shot shell 与 persistent process 继续共享同一个 raw-byte contract。
- Process backend 增加只读 lifecycle mode：`windows-job-object`、
  `windows-taskkill-fallback` 或 `posix-process-group`。ConPTY Job binding 失败时不再
  只停 direct child，而是按 owned PID 调用 bounded `taskkill /T /F`，仍禁止按进程
  名扫描。
- W6 现在同时执行 persistent process 与真实 Job binding，W10 执行 daemon token
  ACL；native Windows smoke 增加 DACL round-trip 和 PowerShell no-BOM UTF-16。
  Linux 聚焦回归为 229 passed / 21 native skips，R1–R12 为 12/12，fresh wheel/sdist
  install smoke 通过。首次 full regression 的 1 个失败暴露了 `state -> runtime`
  依赖违规，根因修复为把 ACL contract 移到顶层中立模块；最终 full regression
  为 2068 passed / 21 native skips / 7 known fork warnings。原生 Windows artifact
  仍是 Release Candidate gate。
# A250 — Windows 自包含 EXE 发布边界（2026-08-14）

- 采用 PyInstaller one-dir + Inno Setup，而不是 one-file 临时解包；保留现有
  Agent Runtime，安装器只承担分发职责。
- EXE 按当前用户安装并拥有自己的 PATH/快捷方式/卸载边界；升级和卸载明确
  不触碰 workspace `.env`、`.nz-coder`、Session、trace、memory 或源码。
- Provider 密钥不进入安装器；首次连接继续使用 `/connect` 的掩码输入、私有
  DACL 持久化和当前 Session 即时切换。
- 新增冻结树资产契约、构建证据、安装→产品命令→覆盖升级→卸载生命周期证据，
  并保留语言服务器为 Doctor 可诊断的可选外部能力。

## A252 — InfCodeX / infcode-dev 终态协议与运行预算融合（2026-08-14）

- InfCodeX 的关键能力不是一处 `CancelledError` catch，而是
  `tool-cancellation -> catch cleanup -> terminal -> snapshot/event` 的有序协议；
  infcode-dev 进一步把已开始的 assistant 标为 `MessageAbortedError`。NZ-Coder
  现在让 Provider/tool 取消先结算未完成 part，再由 ProductionRunLifecycle 写
  `run_end(status=cancelled)`，SessionRuntime 只做一次最终持久化，run middleware
  发 cancelled 而不误报 model/tool failure。
- 真实 TUI 取消验证了 trace 最后一项、Session assistant error、tool part 终态和
  REPL 返回；同时 HTTP legacy abort 合同保持 `message.part.removed` 早于
  `session.run.cancelled`，避免“只修 native、旧入口退化”。
- Broad Test Gate 原来只有 run-local 布尔值，无法区分“用户指定的目录 suite”与
  “模型自行扩大到全仓”。新增结构化 pytest target 提取和 ContextVar scope containment：
  `cron_engine/tests` 可包含其测试文件，但不能授权空目标 pytest、父目录或 sibling。
- InfCodeX 普通 Runner 的默认 20 次 tool-loop 上限与 managed budget 的
  green/yellow/orange/red 区间被组合到 NZ-Coder：默认终端上限为 20，70%/85%/95%
  分别注入一次收敛提示，已发 zone 随 active RuntimeState 持久化；显式用户/评测预算
  仍通过 per-request override 覆盖。
- 真实长任务揭示 headless `--max-turns` 曾停留在 `RunRequest.metadata`，Runtime
  仍使用默认 50，造成 30+ Provider 调用和约 1.1M input tokens。修复后受控真实
  `--max-turns 4` 严格只有 4 次请求，并在第 4 次前产生 yellow trace。这个过程说明
  产品预算必须做端到端 trace 验证，不能只检查 argparse 或单元测试中的 metadata。
- CLI 只在正常完成的 substantial run 后注入 memory reminder；typed cancelled
  result 不再被追加 synthetic user tail，避免恢复时把取消轮误当成待续任务。
- 最终完整回归为 `2126 passed, 21 skipped`；TP-023、TP-024 已有真实产品证据关闭，
  TP-025 保留 verify，等待默认 20 轮下完整长编辑任务达到调用数/工具数性能门槛。

## A253 — 默认 20 轮真实长编辑复测（2026-08-14）

- Session `default20-long-20260814` 在隔离副本完成月份/星期名称解析、CLI、三类测试
  与 README 修改，外部验收 `103 passed`；但 Runtime 用满 20 次模型调用和 26 次
  工具调用，最终明确报告尚未完成整目录回归，因此不能判定终端长任务闭环达标。
- 轨迹显示主要恢复成本不是代码理解，而是用户声明测试范围的自然语言解析：中文逗号
  没有结束 pytest 命令，Broad Test Gate 错误拒绝明确要求的目录 suite；拆分运行又
  揭示目标仓库 CLI 测试硬编码旧 cwd，Agent 为定位和修复消耗到 red budget。
- `declared_test_scopes()` 现在把中英文逗号都视为命令边界，并以失败先行的回归测试
  固化。结论是安全 gate 必须解析“命令边界”，不能只识别 runner 和 path；否则一个
  中文连接句就会让已经正确的 scope containment 在真实产品里失效。
- 修复后的真实 Session `chinese-scope-real-20260814` 在 README 编辑后原样运行
  `python -m pytest -q cron_engine/tests`，得到 `103 passed`；轨迹为 6 次模型调用、
  5 次工具调用且无失败，确认不是只修了 parser 单测，而是完整产品链已经生效。

## A254 — Runtime-Owned Verification Contract + DeepSeek Replay（2026-08-14）

- 对照 InfCodeX `deterministic-evaluator.ts` 的核心思想，把“用户明确写出的验收命令”
  从 prompt 建议提升为 Runtime contract；没有复制 managed task host，而是组合进
  NZ-Coder 已有 RuntimeState、WorkBudget、ToolExecutionContext 和 SessionProcessor。
- `verification_contract.py` 只解析有明确 workspace-relative target 的 pytest 命令，
  拒绝 pathless pytest、绝对/父目录 path 及 pipe/redirect/command chaining。状态记录
  attempted generation、次数、通过状态和有界输出，支持 Session 中断恢复。
- Runner 在 yellow/orange/red 预算区以及提前自然完成边界检查 contract；命令通过正常
  Bash pipeline，因此权限、workspace safety、取消、timeout、trace、result projection
  与模型调用完全一致。同一修改代次不重复执行，新的 edit generation 会重新启用。
- 第一轮真实产品运行证明自动测试本身成功，却暴露 DeepSeek V4 thinking replay 400。
  源码对照确认 InfCodeX `openai.ts` 的 load-bearing 规则是：opt-in provider 的每条
  assistant history 都必须有 `reasoning_content`，没有 thinking 时也发送空字符串。
  NZ-Coder 过去只保留已有值；现在统一在 `message_projection.py` provider-aware 地补齐，
  GPT 等未 opt-in 模型仍剥离该字段。
- 最终 Session `verification-contract-state-fixed-20260814`：4 次 model calls、4 次 tools
  （2 个首轮并行 reads、1 次 edit、1 次 Runtime 自动 Bash），1 行 diff，
  自动命令 `python -m pytest -q tests` 得到 `3 passed`，模型随后正常总结，exit code 0，
  trace 的 contract zone 为 `completion` 且没有 API error。
- 第二轮真实核验发现旧 VerificationManager 仍把推断出的 static command 标成 pending，
  导致 `run_end=completed` 却报告 `verification_state=verifying`。新增明确的 acceptance
  settlement：通过的用户 contract 结算 planner-only required stage，并记录
  `satisfied_by=user_acceptance_contract`。最终 trace 为 `verification_needed=false`、
  `verification_state=passed`、`next_required_stage=null`，状态与行为一致。
- 聚焦回归 47 项和 Ruff 通过；首次 full suite 为 `2139 passed, 1 failed, 21 skipped`，
  唯一失败是架构测试把合法 Runner tool path 数量硬编码为 2。护栏现改为遍历所有调用并
  逐一验证 run-scoped context，不再绑定数量；最终完整回归为
  `2141 passed, 21 skipped, 7 known fork warnings`，耗时 162.62 秒。

## A255 — Long-Task Phase Convergence + Honest Terminal State（2026-08-14）

- 对照 InfCodeX 的 task phase/deterministic evaluator 思路后，真实 trace 证明 NZ-Coder
  的主要问题不是缺一个新框架，而是阶段事实没有成为确定性消费条件：源码证据已充分仍
  探测环境、Provider 轻微偏离 patch schema 就整轮失败、显式验收太晚、最大步数摘要被
  误记成完成。
- 新增窄范围 implementation phase gate：仅对有安全显式验收契约的修改任务，在首次
  edit 前已有 8 次 repository investigation 时拒绝 Bash 环境探测。它把基线的 6 次
  pre-edit shell 回合压到最多 2 次，不限制 structured read/write，也不作用于新项目。
- `apply_patch` schema/handler 支持单文件顶层 `path` fallback，并保留标准 per-hunk path。
  这不是静默猜测目标：只有明确顶层 path 时才继承，所有路径仍逐项安全验证、全部 hunk
  成功后才写入。真实 trace 中四次 `change requires path` 因此消失。
- Recovery 新增 `subprocess_package_root` 与 `workspace_boundary` 分类。pytest 主进程可
  import、CLI subprocess 却 `No module named` 时，直接审查 helper cwd/env 和 package
  parent，不再错误建议读生产源码或运行 pip metadata；越界 workdir 则使用默认 workspace。
- Runner 最后一轮注入 `_MAX_STEPS_PROMPT` 后，若最终文本明确承认达到最大步数，则以
  `max_turns` 终止；最后一轮正常返回 `done/finished` 的短 child 仍是 completed。该边界
  修复了“工具已禁用/任务未完成，headless 却 exit 0”，也避免误伤合法 1-turn 子 Agent。
- WorkBudget yellow 从 70% 提前到 60%，orange/red 保持 85%/95%。这让 20 轮任务在第
  12 轮执行用户契约，而非第 14 轮才暴露回归；最终真实运行在第 12 轮发现 scheduler
  bug，第 13 轮修复，独立验收 105 passed。
- 五次同任务数据从 `20/32 + 不完整` 收敛到 `19/26 + 完整`，最终随机样本为
  `18 model calls / 28 tools + 完整`。因此源码级能力和终态诚实性有实证提升，但没有
  达到 15/25 数值门，TP-025 仍为 verify。下一步应让普通模型执行的同一显式 acceptance
  回写 contract generation，并在非 Git workspace 消费已通过证据，避免通过后的重复
  pytest/diff/compile，而不是继续增加 prompt 或压低 max turns。
- 最终完整回归为 `2151 passed, 21 skipped, 7 known fork warnings`；新增/变更 Python
  文件 Ruff 与全工作区 `git diff --check` 通过。

## A256 — Mutation-Scoped Acceptance Consumption + Tool Schema Closure（2026-08-14）

- 对照 InfCodeX deterministic evaluator 的“确定性结果由 Runtime 消费、Worker 只负责
  下一步行动”原则，补齐了模型主动 Bash 与 Runtime synthetic Bash 的来源边界。显式
  pytest 采用 `shlex.split` 后的严格 token 等价，不接受 pipe、redirect、额外目标或不同
  runner；成功/失败均绑定当前 mutation generation，后续 edit 自动重新激活。
- `AgentLoop` 现在把模型执行的精确 acceptance observation 同步给 VerificationManager；
  Runner synthetic call 带 `_nz_runtime_contract` marker 并保留原手动结算路径，因此 native
  fake service 与生产 Tool Runtime 都不会双计数。证据额外记录 `source`/`zone`：budget-zone
  pass 是中间证据，model-issued 或 completion pass 才能触发最终总结提示。
- 第一轮真实 trace 暴露“parser 刚改完、测试和 README 未做，yellow 旧 59 项却通过”的
  假收口。RuntimeState 因此直接观察并持久化 Todo open count：预算区在 Todo 未完成时
  defer，completion 仍强制执行。没有 Todo 的任务不伪造完成度，预算区 pass 只提醒继续
  outstanding requirements。
- 三轮相同 59-pass baseline 长任务分别为 20/31、20/29、20/30，全部诚实返回 max_turns；
  独立结果为 94/108、91/92、93/107。三轮没有达到 TP-025 的 15/25 与全绿门，说明状态
  消费正确不等于规划和补丁生成已经成熟。
- 第二轮真实 trace 有 6 次 `apply_patch` change 缺 path。根因不是 handler 不兼容，而是
  provider schema 的嵌套 item 没把 path 声明为 required；DeepSeek 因此合法地产生省略
  path 的对象。schema 收紧后第三轮该失败降为 0，证明源码级对齐必须检查 provider 实际
  看到的 JSON schema，不能只检查 Python handler signature。
- 第三轮剩余两次 patch 失败都是“向测试文件末尾追加内容”却复制错一个 docstring 引号。
  `apply_patch` 新增路径安全、事务化、原子验证的 `op=append`，Recovery 只在纯 EOF 追加
  场景推荐它；replace/create/delete 语义与 top-level single-file compatibility 保持不变。
  该项有单元红绿证据，尚无第四次真实长任务证据，文档不把它写成已验证性能收益。
- subprocess package-root 诊断现在输出 active workspace root 与检测到的 package directory，
  明确要求使用“包含 package 的目录”作为 cwd，不再让模型猜 `parents[...]`。真实三轮仍
  表明 DeepSeek 对 dirname/parents off-by-one 很敏感，下一阶段应把 package-root 修复做成
  结构化 workspace evidence，而不是继续堆自然语言说明。
- 最终完整仓库回归为 `2173 passed, 21 skipped, 7 known fork warnings`，Ruff 与
  `git diff --check` 通过。TP-025 继续 `verify`；剩余重点是减少首轮读取、让追加型 patch
  使用稳定 op，以及在无 Todo 时建立可验证的 requirement/artifact completion state。

## A257 — Contract-Led Runtime Convergence（2026-08-14）

- 本轮没有继续复制某个仓库的目录形状，而是把 InfCodeX 的 deterministic evaluator、
  task contract 与 infcode-dev/OpenCode 的 provider projection、context/tool-schema 边界
  组合进 NZ-Coder 现有 Native Runtime。关键原则是：计划仍只调用模型一次，Runtime
  持有可验证事实，模型负责语义决策，不新增线性 workflow host。
- `TaskContract` 把需求分成 artifact/test/verification/compatibility/documentation/mixed，
  `RequirementLedger` 将文件写入、目标测试与 exact acceptance 绑定到 mutation generation。
  写入只能让行为需求成为 candidate；静态检查或无关测试不能冒充语义完成；后续编辑会
  使旧验证证据失效。`CompletionGate` 因此可以独立于 Todo 阻止“只改了部分文件就总结”。
- `ProjectExecutionFacts` 将 workspace root、project root、source/test roots、Python 包的
  module name/package path/module cwd、Node package 与验证命令变成结构化事实；
  `ImplementationBundle` 只在中高复杂度多产物任务首轮注入，并保持有界，避免把 Repo Map
  全量塞进 prompt。
- Verification scheduler 不再在每个预算区重复跑用户整套验收：yellow=static、
  orange=targeted、red=convergence，只有账本无硬缺口时 red 才可 exact；completion 始终
  exact。通过后 Runner 直接保留已有 final text，避免“测试已绿还再问模型一次”。
- WorkBudget 的产品默认值收口为 13 normal + 2 closure，20 仍是 emergency hard cap。
  closure 的工具可见性与执行策略同时收窄，最后一轮仍允许一次已知路径修复，而不是把
  tools 全关掉后要求模型用文本假装完成。
- 工具 durable message 增加 resource/evidence/generation metadata；provider projection
  只压缩已经被后续 mutation 或成功验证取代的证据，Session 原始消息不改。这样既保留
  trace/恢复能力，又防止模型反复依据旧文件或旧失败做决策。
- Provider schema 在 model-facing boundary 深拷贝适配，DeepSeek 简化 `anyOf/oneOf/allOf`
  和冗余描述时仍保留递归 `required`、enum；recursive linter 能发现嵌套 item 与 handler
  所需字段不一致。Canonical registry 与工具 handler 没有 provider 特例。
- 聚焦回归 `218 passed`；完整仓库回归 `2212 passed, 21 skipped, 7 known fork warnings`。
  fake-provider 证明 planner 没有新增调用、自然结束边界的 exact acceptance 能进入最终
  Session 且无需第三次模型调用；Ruff 与 `git diff --check` 通过。
  外部 DeepSeek 同任务 A/B 明确延后，因此 TP-025 仍是 `verify`，不能用静态架构完成度
  代替真实调用数、token、wall time 或 SWE-bench 成绩。

## A258 — Contract-Led Runtime 真实 Provider 反证（2026-08-14）

- 同一 `59 passed` fixture 和同一长任务运行两次。默认配置得到 20 execution calls / 32
  tools、493,135 tokens、独立 94/100；显式开启 planning 得到 20/28、417,010 tokens、
  独立 80/85。两轮都 `max_turns`，因此 A257 的模块和 fake-provider 绿灯不能视为产品闭环。
- 根因不是 RequirementLedger 规则本身，而是它没有获得 contract：默认
  `NZ_PLANNING_ENABLED=false`；手动开启后 DeepSeek 返回的 3672 字符 planner JSON 在字符串
  中截断，fallback 保存 plan 文本却清空 contract。两轮均没有
  `implementation_bundle_ready`，ProjectExecutionFacts 自然也没有纠正 CLI subprocess cwd。
- 开启 planning 的运行除 20 个 execution calls 外还发生 1 次 planning 与 2 次 replanning；
  现有 `llm_request`/headless usage 没有统计这些控制面调用。以后“模型调用数”必须区分
  execution turns 与全部 Provider calls，不能只挑较小的数字报告。
- Runtime 的正面证据是：workspace 外 Bash 被拒绝、自然/硬上限验收确实运行、失败终态
  为 `max_turns` 而非伪 completed。负面证据是 contract 主链未激活、首 6 轮仍读 12 个文件、
  package root 恢复失败和 closure 不足。下一步优先修 planner contract 的默认启用与截断
  恢复，再做第三轮同题 A/B；此时 TP-025、TP-028、TP-029 均不能关闭。

## A259 — Default Contract Activation + Purpose Accounting（2026-08-15）

- 参照 InfCodeX deterministic contract 的原则，把 contract owner 从可选 planner 移回
  Runtime：存在安全精确验收命令时，默认产品路径零调用生成保守 contract；合法 planner
  只做 enrichment，截断 JSON 保留 bootstrap contract。这样不再用 feature flag 决定硬
  完成语义，也没有为了“启用规划”额外购买一次模型调用。
- contract 结构本身可激活 ImplementationBundle；ProjectExecutionFacts 能识别唯一嵌套
  Python project，并输出正确 project root、module cwd、test root 与 test command。
  ModelGateway 的 buffered/streaming completion、planning/replanning、stall sidecar 统一通过
  purpose observer 进入 RuntimeState 和 headless metadata，控制面 usage 只加入 RunContext
  一次。自然完成的组合测试证明 exact acceptance 先更新 ledger，再由 CompletionGate 决策。
- TDD 红绿证据覆盖默认 bootstrap、planner malformed fallback、rich contract bundle、嵌套
  project facts、buffered/streaming/sidecar accounting 和 headless output。聚焦 `215 passed`，
  完整仓库 `2224 passed, 21 skipped, 7 warnings`，Ruff 与 `git diff --check` 通过。
- 第三轮相同真实任务从 `59 passed` baseline 开始。trace 确认
  `task_contract_bootstrapped=1`、`implementation_bundle_ready=1`，所以 A258 的默认激活缺陷
  已被真实 Provider 关闭；headless 报告 20 个 coding calls、20 attempts、699,514 Provider
  total tokens。不过运行仍为 20 calls / 35 tools / max_turns，独立验收只有 92 passed、5 failed。
- 5 个失败全部来自 CLI test helper 保留旧绝对 cwd，子进程导入旧 fixture。模型已经读到
  helper，却走向越界 `cd` 和 editable pip install。bootstrap 没有 expected artifacts，bundle
  因此 candidate_count=0；正确 project facts 没有转化成一行确定性 cwd 修复。
- 新的 Runtime 反证是 hard cap：模型最后仍返回 tools 时，Runner 没有进入 natural-stop exact
  acceptance 路径，最终 contract attempts=0、ledger pending、final text 为空。pytest 管道又
  因缺少 pipefail 把失败命令呈现为 shell success，Recovery 没有注入 stale-workspace
  subprocess 诊断。TP-025/TP-028 不能关闭；后续先修 hard-cap acceptance settlement 和
  pipeline/subprocess recovery，再增强零调用 candidate/artifact inference。

## A260 — Deterministic Terminal Boundary + Bootstrap Evidence（2026-08-15）

- 这次没有继续增加 prompt 或 planner，而是修复第三轮 trace 已证明的控制流事实。参考
  InfCodeX 的 deterministic evaluator 思路，Runner 现在把 natural、buffered tools、streamed
  tools 与 exhaustion 都投影到同一 Terminal Boundary：exact acceptance、ledger observation、
  completion decision、persistence 的顺序固定，最后一轮工具调用不再绕过验收。
- `15 coding calls` 是产品 SLA，`20` 只是证据充分的局部修复安全帽。第 16-20 次调用改名为
  `bounded_emergency`，进入条件必须同时满足 diff、失败证据、已知目标、无需广搜；工具 schema
  和执行 policy 同时只留已知路径 read/edit、diff 与定向/精确验证。runaway model 没有这些
  事实时在 15 次结束，不再无条件消耗 20 次。
- Bash 的 canonical outcome 来自 `ToolOutput.metadata.exit`，可见文本只负责 UI/模型投影；
  POSIX Bash 通过 pipefail 保留 pipeline 上游 pytest 的退出码。这个边界同时进入 parent 与
  child executor，避免子 Agent 继续沿用字符串前缀判断。
- 新增的 subprocess workspace diagnostic 不执行测试代码，只解析失败 helper AST 中有限的
  Path/cwd 表达式。它能识别“旧 fixture 仍可 import，但 cwd 指向旧 workspace”的情况，直接
  给出 helper、旧/新 cwd 和 module，避免错误走向 pip install 或 production patch。
- BootstrapArtifactResolver 在 RI/LSP/embedding 之前，根据显式路径、唯一 stem、请求测试
  surface、README 与入口点生成分级证据。cron fixture 的 parser、三个 test 文件、nested
  README 成为 hard artifacts，scheduler/CLI/entrypoint 只是 candidates。Ledger 相应收紧：
  acceptance 可直接支持 artifact-free behavior/compatibility/verification，但 docs/artifact/test
  必须先有对应 mutation evidence。
- G1-G7 均以先失败后实现的测试固定；本节记录的是本地源码闭环，不冒充第四轮真实 Provider
  成绩。下一步必须先通过完整 pytest、Ruff 与 diff review，再决定是否花费真实调用成本。

## A261 — 第四轮真实反证：Terminal 已收口，但补丁生成与恢复排序未达产品门（2026-08-15）

- 相同 59-pass cron fixture 的第四轮结果是 20 coding calls、1 stall sidecar、32 个工具调用、
  930,399 个互斥 bucket tokens，独立验收 `34 passed / 61 failed`，终态 `max_turns`。所以
  A260 的确定性边界通过了真实链路，但 15/25/全绿产品门仍然明确失败。
- Terminal Boundary 与 InfCodeX-style evidence ownership 已得到正面实证：最后一轮仍有工具时
  Runtime 在 generation 5/6 都运行 exact acceptance，contract attempts=2，ledger 保持
  unresolved，失败没有被 summary 或模型措辞覆盖。说明这轮不是终态 false-pass，而是补丁
  本身真实错误。
- 最终根因只是 parser 循环多出一行 `expanded.append(vals)`。DeepSeek 把一处实现拆成多轮
  replace/read/static micro-step，py_compile 只能发现中间 SyntaxError，不能发现字段错位；直到
  第 15 call 才跑语义测试。当前最直接的对齐方向是更早消费聚焦测试证据和减少同文件往返，
  不是引入新的 planner/router call。
- subprocess workspace drift 诊断本身是正确的，但在 CLI/parser/scheduler 同时大面积失败时
  优先级错误。参照 InfCodeX“先消费全局确定性 evidence，再做局部 recovery”的思想，现改为
  多测试文件失败先报告 `widespread_test_regression`，要求检查本轮共享 production surface；
  精确 target 只保留每文件一个、最多三个，避免几十条失败节点污染 verification context。
- bootstrap 的 slash token 误判和 synthetic flag 可复制问题说明：确定性字段存在不等于它们
  可以被信任。现在 artifact 必须是现存文件或明确文件名；`_nz_runtime_contract`/
  `_nz_runtime_verification_stage` 只有与 stored contract/classifier 一致才有权限。模型复制内部
  字段不能重新开放 emergency shell exploration。
- ModelGateway 的 usage buckets 本来互斥，但 RunResult total 仍沿用 raw input+output 旧语义，
  导致第四轮 headless 顶层显示 722,619，而 purpose total 为正确的 930,399。TokenUsage total
  已统一为 input/output/reasoning/cache-read/cache-write 五桶之和，后续产品与 trace 数字一致。
- 本轮没有进入 DeepSeek Harness-style Code Mode：20 calls 中可确认的浪费主要来自 2 次额外
  首轮探索、同 parser 的多轮 read/edit micro-step、以及错误 recovery 优先级。先验证这些
  确定性修复；只有仍存在大量必要的 LLM→tool 往返，才有 Code Mode 的证据基础。
- 后续确定性修复通过 `158` 项聚焦回归；完整仓库为 `2247 passed, 21 skipped, 7 known
  fork warnings`（114.88 秒），Ruff 与 `git diff --check` 通过。真实第四轮仍按失败记录，
  没有用这些本地绿灯改写它的产品结论。

## A262 — 第五轮：suite 全绿不等于产品门或兼容契约通过（2026-08-15）

- 新隔离 fixture 由 `59 passed` 开始，第五轮最终为 19 coding calls、1 stall sidecar、31
  tools、871,743 tokens、207.10 秒；Runtime exact 和外部独立 suite 都是 `102 passed`，状态
  `completed`。相较第四轮 `34/95` 是真实能力提升，但仍违反 15/25 性能 SLA。
- InfCodeX-style contract evidence 本轮完整工作：parser、三类 test、README mutation evidence
  齐全，generation 4 acceptance 失败、generation 5 acceptance 通过，ledger 七项才一起转为
  satisfied。这证明 Terminal settlement 修复已穿过真实 Provider，不再是 fake test 结论。
- recovery 不能只选择一个全局标签。CLI stale cwd 是 AST 可证明的局部事实，scheduler hour
  快速路径是共享 production failure；`widespread_test_regression` 覆盖前者后，模型浪费两个
  emergency calls 探测旧目录。更合理的对齐是 composite evidence：同时给出已证明的 helper
  drift 和跨文件 failure fanout，让模型直接做两个已知目标修复。
- 102 项测试是 Agent 自己扩出的 suite，仍存在共同盲区。原数字 `5-1` 从拒绝变成环绕接受，
  破坏“保持现有数字 API”；名称环绕步长 `FRI-MON/2` 又使用数值差而非序列位置计算，结果
  `[0,1,5]`。这说明 RequirementLedger 的 compatibility requirement 不能只凭整个新增 suite
  PASS；应把 baseline behavior probe 或原测试集差分证据纳入 compatibility satisfaction。
- Terminal settler 已产生 deterministic content，但 Lifecycle raw result 没持久化
  `content_text`，所以 headless `text` 为空。运行时控制流正确不代表产品 envelope 正确；
  InfCode/OpenCode 的 server/session result 边界值得继续借鉴，终态正文必须成为 durable result
  field，而不是只通过 UI callback 临时投影。
- 下一步仍不是 Code Mode。可确认的多余成本包括：首次编辑前 4 calls/11 tools、两次错误
  append anchor、一次 non-Git `git diff`、以及两个被 emergency gate 拦截的目录探测。先修
  early targeted verification、append schema consumption、composite recovery 和 result envelope，
  再看是否还剩大量不可消除的必要 LLM↔tool 往返。

## A263 — Terminal efficiency closure 与第六至八轮真实反证（2026-08-16）

- 本轮组合 InfCodeX 的 authoritative `RunResult.output` / deterministic evaluator 与
  infcode-dev/OpenCode 的 Assistant terminal persistence 顺序，而不是增加新的 Host。Lifecycle
  现在把 terminal content 写入 Assistant、`last_status.content` 和 headless envelope；tool-call
  边界不会再把“接下来更新 README”之类过渡文本当最终回答，而是生成只陈述持久事实的摘要。
- 失败恢复从互斥早返回改成 specificity 排序的 composite diagnostic。AST 可证明的
  `subprocess_workspace_drift` 成为 primary，`widespread_test_regression` 可作为 supporting，
  helper path 成为 durable repair target；新诊断先清空旧分类，避免普通 parser 失败沿用前一轮
  workspace-drift 标签。
- Runtime 只在成功写入后推进 mutation generation，并额外记录
  `source_mutation_generation`。static/targeted stage 因生产源码变化才重新调度；测试或文档修改
  不会反复执行较弱验证，exact 已失败后的 test-only repair 直接回到 exact。非 Git workspace
  在 orange/red/closure 阶段拒绝无效 `git diff/status`，引导使用 `diff_status` 与
  `verify_changed_files`。
- 有安全 exact contract 的修改任务在首次编辑前收集 6 次结构化调查后，下一次 Provider 请求
  隐藏更多调查工具，只保留写入、Todo 与验证工具。该限制只作用于尚无 mutation 的
  bugfix/feature/refactor/test，不影响无 contract、新项目或编辑后的定向恢复。

三轮都从独立 `59 passed` fixture、相同中文任务、DeepSeek V4 Flash、auto 权限和 20-turn
hard cap 开始，且用外部进程重跑 Agent 新增后的完整 suite：

| Session | coding / sidecar calls | tools | Provider tokens | 独立 suite | 终态正文 |
|---|---:|---:|---:|---|---|
| `efficiency-sixth-real-20260816` | 19 / 4 | 37 | 628,234 | 97 passed | 过渡文本，促成后续修复 |
| `efficiency-seventh-real-20260816` | 16 / 0 | 26 | 529,028 | 96 passed | durable factual summary |
| `efficiency-eighth-real-20260816` | 15 / 0 | 24 | 455,057 | 92 passed | durable factual summary |

- 第八轮首次达到 TP-025 的 `<=15 coding calls / <=25 tools` 数值门；相比第六轮减少 4 次
  coding calls、13 次 tools 和约 173k tokens。trace 只有一次 static、一次 targeted，
  `emergency_broad_exploration=0`，无 non-Git `git diff/status`。
- 该结果不能外推为所有任务稳定达标：第七轮仍为 16/26，说明 Provider 行为有一轮波动。
  额外兼容探针确认数字 `5-1` 继续按旧 API 拒绝、`0/7/SUN` 等价，但三轮生成补丁都没有支持
  `FRI-MON/2` 跨周名称范围。因此 TP-025 继续 `verify`，且不能只用新增 suite 全绿宣称语义完整。
- 本地 TDD 同时修复了 synthetic verification 被计入 child Provider iterations 的观测错误；
  合成 Assistant Part 仍保留在 Session/trace，但 route facts 只统计真实 Provider Assistant 回合。
- 最终静态门为 compileall、Ruff 与 `git diff --check` 全绿；完整仓库回归为
  `2267 passed, 21 skipped, 7 known fork warnings`，耗时 151.40 秒。一次中间全量运行出现
  Workflow 并发时序用例 flake，隔离连续 5 次和最终全量均通过，未将其误归因为本轮 Runtime 回归。

## A264 — Mutation-Scoped Semantic Evidence 与 Provider 自愈（2026-08-17）

- 第六至八轮的外部探针证明了一个架构漏洞：exact pytest 可以满足没有 artifact 的
  compatibility requirement，导致 Agent 自己新增的 suite 即使遗漏 `FRI-MON/2`，Runtime
  仍会直接完成。源码对照后采用 InfCodeX 的三层边界：deterministic evaluator 只拥有命令
  真值，TaskVerificationContract/Sidecar 拥有语义标准，Terminal Boundary 只消费两类证据；
  同时保留 infcode-dev/OpenCode 的 step snapshot → patch → normalized terminal 顺序。
- `TaskContract` 升级到 version 2。compatibility requirement 无论 planner 是否显式给出空
  数组，都会要求 `semantic_review`；exact acceptance 只能把它推进到 `candidate`。真实
  verifier accept 为当前 mutation generation 写入 `semantic_review_passed`，后续任何 mutation
  都会使该证据失效。provider error、timeout、无 tool call 等 fail-open accept 不能成为证据。
- Sidecar 现在获得 task objective、逐项 requirement/required evidence、constraint、当前代 exact
  acceptance 状态和最多 6 KB 的真实 unified diff。自然停止和 nominal tool-batch settlement
  共用同一 completion verifier，review 后重新计算 ledger；accept/revise/unavailable 分别进入
  独立 trace reason，不再让工具终态绕过语义审查。
- 真实第九轮使用规范化过硬编码 CLI cwd 的独立 `59 passed` fixture，Session
  `semantic-closure-ninth-real-20260817`。Agent 补丁本身达到 `101 passed`，外部探针确认数字
  `5-1` 继续拒绝、`0/7/SUN` 等价、`FRI-MON/2 -> [0,5]`、`JAN-MAR/2 -> [1,3]`，关闭了前三轮
  的具体语义盲区。但原运行仍诚实结束为 `max_turns`：20 coding calls、6 sidecar attempts、
  28 tools、951,595 tokens，R6 留在 candidate，没有伪造 completed。
- trace 证明 6 次 sidecar 都在约 70–123 ms 内得到 400：
  `Thinking mode does not support this tool_choice`。InfCodeX 的 OpenAI Provider 已有同能力的
  forced-tool-choice fallback；NZ-Coder 因此在 buffered/streaming Gateway 增加一次兼容重试：
  保留 tools、移除 named tool choice，并发出 `model_call_tool_choice_fallback`。无关 400 仍不重试。
- 真实端点进一步显示固定 1024 verifier 输出预算会被 DeepSeek reasoning 全部吃掉并以
  `finish_reason=length` 结束。对 `deepseek-v4*` 的结构化 verifier 请求现在使用该端点支持的
  `thinking={type: disabled}`，仍保持 1024 bounded output。用第九轮完整 transcript、diff 和
  contract 做后置真实审查，2.63 秒得到 `verifier_ok/accept`，一次调用、184 output tokens，
  不再经过 400 fallback，也不是 synthetic verdict。
- 同一真实启动还发现帮助宣称 `nz-coder run --prompt TEXT`，headless parser 却只接受位置参数。
  `-p/--prompt` 已成为正式入口，并可与位置参数/stdin 按稳定顺序组合；帮助输出使用
  `--prompt TEXT`，不再显示内部 dest 名。
- 本轮没有重写第九轮原始失败状态，也没有重新购买整轮 coding calls。post-fix 证据由真实
  Sidecar 请求和本地 Terminal settlement 回归共同组成；只有后续全新端到端任务也在首次
  semantic review 后稳定完成，TP-025 才能从 `verify` 关闭。
- 最终聚焦链为 `154 passed`；完整仓库回归为 `2282 passed, 21 skipped, 7 known fork
  warnings`（119.45 秒）。Ruff、compileall 与 `git diff --check` 全部通过。

## A265 — Bounded Revise、Stall Ownership 与 Terminal Truth 收口（2026-08-17）

- 本轮继续做源码行为对齐，不把目录或类名相似当作完成。InfCodeX 的
  `stall-detector.ts` 只观察 Assistant tool use，`stall-sidecar/orchestrator.ts` 每次只做一个
  bounded review cycle，`primitives/runner.ts` 保留 20 total loop 与 2 次 stop-hook reanimate；
  infcode-dev/OpenCode 的 `session/processor.ts`、`context-budget.ts` 和
  `agent-loop/stability.ts` 则把 durable Session、context pruning 与终态稳定性分开。NZ-Coder
  因此没有盲目增加 turn cap，而是修 ownership、反例检测、缺项反馈与事实终态。
- 第十六轮虽 `114 passed/completed`，外部反例仍证明 `FRI-MON/2` 错误；第十七轮修好名称
  step 后又放宽 numeric `5-1`，且 20 coding calls 后 `max_turns`；第十八轮因
  package-root repair target 丢失在 16 calls 提前终止；第十九轮 99 tests 通过但 README
  写错层级、名称 step 再回归，并在 `max_turns` 中透传模型的虚假完成文本。这四轮再次证明
  “测试绿/模块存在/模型说完成”都不能替代独立行为探针和 Runtime-owned evidence。
- Sidecar deterministic compatibility review 现在识别 alias normalization 与 index step 的
  先后顺序，也识别旧 numeric descending guard 被 conjoined field wrap gate 吞并。合法的
  ordered de-dup 不误报；确定性风险会与 LLM revise 原因聚合，且在风险已确定时短路本代
  Provider judge，避免为确定结论再付一次模型调用。
- StallDetector 的输入所有权与 InfCodeX 对齐：只有 call id 与 canonical marker 同时证明是
  Runtime verification 的工具调用才排除；模型复制内部 marker 仍按普通 Assistant call 观察。
  第十七轮 trace 证明第十六轮那两次 Runtime acceptance 假 stall 已消失。
- Recovery 把 `subprocess_package_root` 的 failed helper 变为 durable repair target，并阻止
  通用 `No module named` 覆盖“已知局部修复”事实。这样 bounded emergency 消费的是结构化
  evidence，不是模糊错误字符串。
- Completion feedback 从 legacy adapter 下沉到统一 CompletionGate helper，并由 Native Runner
  在 natural boundary 消费。提示包含精确 expected artifact，按 mutation generation + missing
  IDs 去重；名义边界第一次发现缺项时可有界 reanimate。legacy-backed CLI 与 native SDK
  因此共享同一事实，不再出现 Native/Legacy 能力分叉。
- `max_turns` 结果统一为 Runtime-owned deterministic summary。自然停止也不能保留模型自报的
  “全部完成”；摘要按当前 mutation generation 区分 exact acceptance passed/failed，并列出
  unresolved IDs。这对应 InfCodeX deterministic evaluator 与 OpenCode durable terminal result
  的职责分离，而不是复制 TypeScript 表面结构。
- 聚焦链为 `130 passed`；完整仓库为 `2304 passed, 21 skipped, 7 known fork warnings`
  （117.18 秒），Ruff、compileall 与 `git diff --check` 全绿。尚未做新的付费端到端运行，
  所以 TP-025/TP-035 保持 `verify`，不把本地门禁写成产品稳定性结论。

## A266 — CompletionGate Bounded Reanimation 与 Durable Terminal Truth（2026-08-17）

- A265 的 generation-scoped 单次提示解决了“完全没有缺项反馈”，但对第十九轮 trace 做控制流
  重放后发现仍会空转：模型忽略第一条提示再次自然停止时，相同 signature 不再注入 user turn，
  Runner 又因未到 nominal turn 15 继续调用。参照 InfCodeX Runner 的
  `stopHookReanimateBudget=2`，CompletionGate 改为全 run 最多两次 correction；第三次无新证据
  立即 `max_turns`，reason 为 `completion_gate_reanimate_budget_exhausted`。这不是提高 hard cap，
  而是把隐藏的 11→15 空转压缩成有界协议。
- Native Terminal Boundary 与 legacy `_PolicyService` 消费同一个
  `COMPLETION_GATE_REANIMATE_BUDGET`。adapter 在预算耗尽时不再返回 `continue`；review stop 与
  ordinary unresolved stop 都生成 Runtime-owned failure summary，模型“已完成”正文不能越过任一
  分支。红绿测试分别复现了三次 continue、adapter continue 和 review false text 三个旧行为。
- CompletionGate 不再把所有 unresolved requirement 都当成 Worker action。当前代已有
  `verification_passed`、只缺 `semantic_review` 的项目进入 Runtime-owned evidence 分区，明确
  “不要为此单独改代码”；文档/artifact/test 等可执行项仍给出精确 expected path。这个边界与
  InfCodeX Worker/Sidecar Evaluator 分工一致，也避免 R5 未完成时主 Agent为 R6 再改 parser。
- 新的 mixed-ledger Runner 集成测试覆盖：错误层级 README → synthetic R5 指引 → 正确 README
  mutation → 当前代 exact acceptance → R6 semantic-only → verifier evidence → completed。它验证
  的是一次 Runner 内的数据流，不是几个互不相连的 helper 单测。
- Terminal truth 继续向产品边界收口。natural、streamed tool terminal、buffered tool terminal
  的 `max_turns` 都把配置 cap 交给 Lifecycle；有 deterministic content 时 UI 直接展示事实摘要，
  不再用 `Agent stopped after reaching max_turns=None/N` 覆盖。`max_turns` 还会把最后 durable
  Assistant content 替换成同一摘要，因此 headless、TUI、Session resume 三个消费者看见同一真值。
- 聚焦链（含 Loop fake、Lifecycle、Sidecar、Recovery）为 `221 passed`；完整仓库为
  `2310 passed, 21 skipped, 7 known fork warnings`（112.57 秒）。没有新外部 Provider run，
  TP-025/TP-035 仍为 `verify`，TP-038 仍是 `fixed locally，待真实复测`。

## A267 — 第十九轮续跑反证：Contract Input 与 Tool-Batch Terminal Protocol（2026-08-18）

- 本轮没有创建新 fixture，也没有重跑原任务；使用第十九轮同一 workspace 和 Session
  `semantic-closure-nineteenth-real-20260817` 做 8-turn continuation。运行 116.18 秒，8 次
  coding Provider calls、12 个 tool events（其中 1 个 Runtime static verification）、
  536,527 个互斥 bucket tokens，终态为 `max_turns`。这符合“只续之前进度”的测试口径。
- Agent 的实际代码结果已明显好于终态：指定的 `cron_engine/README.md` 得到更新，独立 suite
  从 99 增至 `109 passed`；外部探针确认 numeric `5-1` 继续拒绝、`0/7/SUN` 等价、
  `FRI-MON/2 -> [0,5]`、`FRI-MON/3 -> [1,5]`、`JAN-MAR/2 -> [1,3]`。因此这次
  `max_turns` 不是补丁错误，而是 Runtime 没能消费已经存在的验收事实。
- trace 给出直接根因：`ProductionRunLifecycle.last_user_text()` 把真实 continuation 硬截到
  300 字，恰好只保留到单词 `prese`。尾部“regression coverage +
  `python -m pytest -q cron_engine/tests`”全部丢失，导致 `task_mode=unknown`、
  `wants_tests=false`、declared test scopes/VerificationContract/TaskContract/RequirementLedger 全空。
  模型随后两次运行同一精确目录命令都被 Broad Test Gate 拒绝，又在 non-Git workspace 调用
  `verify_changed_files` 失败，最终耗尽第 8 轮。
- Contract ownership 现在与 retrieval query 分离：Lifecycle 保留完整真实 User instruction；仅
  memory/repo retrieval 使用已有 300 字有界投影。英文未加反引号的 pytest 命令解析也改为
  选择“最长且所有位置参数均为 workspace 内 test path”的安全前缀，因此句尾
  `Do not claim completion...` 不会再被误吞为测试目标。相同 606 字输入已独立投影为
  `task_mode=test`、scope `cron_engine/tests`、精确 VerificationContract 和包含
  docs/compatibility/semantic_review 的五项 contract。
- A266 的 terminal truth 在真实 headless envelope 中已生效：模型没有再次留下“全部完成”，
  JSON `text` 为 Runtime 的 `Stopped at the work limit without claiming completion...`。但
  Session 结构暴露了更细的协议 bug：Runtime 把这段正文写进最后一个仍带
  `verify_changed_files` tool call 的 Assistant，后面仍跟对应 Tool result 和 synthetic
  diagnostic。正文真实不等于消息顺序合法。
- Terminal persistence 现在只复用没有 `tool_calls` 的 settled Assistant；若终态发生在 tool
  batch 边界，则在全部 Tool results 之后追加独立、带 message identity/parent/time/end-state
  和 text part 的 Runtime Assistant。旧 tool owner 保持空 content 与原 tool call，不再产生
  “看起来已经终止但后面还有工具结果”的 durable transcript。
- 上下文成本也按源码核对，而不是凭 token 数猜测。该 Session 的 content 约 106K chars、
  tool-call arguments 约 38K chars；新 run 从约 40K input 增至 64K。trace 中没有 prune/compact，
  因为会话恰有两个真实 User turns。NZ-Coder 的 recent-two-turn 保护与 infcode-dev
  `SessionCompaction.prune` 反向扫描到第二个 User 前不裁剪的规则一致；当前证据不支持为降成本
  擅自改成只保留一轮。TP-025 继续 `verify`，后续应评估 terminal/max-turn continuation 的
  显式 summary boundary，而不是破坏正常两轮推理证据。
- 三个根因均走过红绿：长 continuation 尾部 scope 丢失、英文命令后 prose 误吞、tool owner
  被终态正文覆盖最初共同得到 `3 failed`；实现后相关 Lifecycle/Contract/ExecutionContext/
  RuntimeState 链为 `80 passed`。第一次全仓验证暴露 queued-followup 旧测试仍要求把
  `interrupted` end-state 写在 tool owner；协议断言改为“settled tool owner + 独立 terminal
  Assistant”并补齐空正文分支后，最终全仓为 `2313 passed, 21 skipped, 7 known fork
  warnings`（111.41 秒）。Ruff、compileall 与 `git diff --check` 全绿。

## A268 — Unfinished-run Continuation Boundary（2026-08-24）

- A267 的 8-turn 续跑不是模型单次异常：durable Session 有 71 条消息，Provider view 每一轮仍
  重放上一段未完成运行的全部 tool transcript，input 从约 40K 增至 64K，累计 536,527 tokens。
  ContextManager 没有失效；当时会话尚未达到 model-aware compact 阈值，而且 recent-turn 保护本来
  就不应被粗暴删除。真正缺少的是 `max_turns` / `interrupted` 的显式 continuation boundary。
- 源码对照采用 InfCodeX `primitives/compaction.ts`、`auto-resume.ts` 的 anchored summary / bounded
  resume 思路，以及 infcode-dev/OpenCode `projectors.ts`、`context-budget.ts`、`compaction.ts` 的
  durable Session 与 Provider projection 分层。NZ-Coder 没把参考项目的 TypeScript 表面结构硬搬
  进来，而是在已有 Lifecycle、message projection 和 ContextManager 边界内完成同一职责分离。
- Lifecycle 在未完成终态的独立 Runtime Assistant 上写入 `_nz_continuation`。summary 是确定性的，
  不调用付费模型，最多 6,000 字；包含完整且有界的最新真实 User instruction、目标、未解决
  Requirement、验收标准、修改文件、repair target、验证证据、最后失败与下一步。durable User
  message 是任务权威来源，优先级高于旧 runtime state 中历史遗留的 300 字 `initial_task_text`。
- 下一条真实 User 到来时，只在 Provider view 中把旧 prefix 替换为
  `<continuation-context>`，随后原样放入 `<current-user-instruction>`。旧 summary 中的控制标签会
  转义，当前 User 明确拥有最高权限；durable transcript 本身不删、不改，正常 `completed` 边界
  也不会启用该投影。这样审计/恢复仍有完整事实，模型却不必反复购买旧工具输出。
- ContextManager 检测到 active boundary 后不再对已经从 Provider view 隐藏的 durable prefix 做
  time-based micro-compaction，避免“模型没看到但存档被悄悄改写”。trace 分别记录 context boundary
  与一次性的 provider projection，包含 status、丢弃消息数和 summary 字符数。
- 用 A267 同一份 817 KB Session 做纯离线投影测量：provider messages 从 68 降至 1，估算 input
  从 60,535 tokens 降至 573，减少 99.05%；boundary 为 1,823 字，606 字真实续跑要求末尾的验收
  命令仍保留。该数字只证明 projection 成本边界，不等价于新的 Provider/SWE-bench 成绩。
- 本轮按 root-cause-first 与 TDD 收口：先复现 prefix 重放、legacy task 截断、标签注入、重复 trace
  和 hidden-prefix micro-compaction，再实现边界。首次全仓验证的唯一失败是架构守卫发现
  `_sanitize_messages` 超过 20 行；trace 被提取为独立方法后门面恢复为薄投影入口。没有重新购买
  DeepSeek 整轮调用，产品状态仍需一次低成本真实 resume 复测后才能把 TP-041 关闭。
- 修正架构守卫后完整仓库回归为 `2319 passed, 21 skipped, 7 known fork warnings`
  （150.93 秒）；Ruff、compileall 与 `git diff --check` 也全部通过。

## A269 — 真实 Continuation 复测与 DeepSeek Wire Boundary 修复（2026-08-24）

- 在 `/home/pyh/test_nzcoder/.continuation-boundary-smoke-20260824` 创建只读小型 fixture，使用
  Session `continuation-boundary-real-20260824` 和显式 `--max-turns 1` 做低成本真实端点测试。
  首次运行按预期持久化 717 字的 `_nz_continuation`，但 Provider 在 357 ms 内返回 400：
  `reasoning_content in the thinking mode must be passed back`。0 usage 不是低成本成功，而是请求
  在生成前被拒绝；原 trace 和 Session 均保留该失败事实。
- 数据流回溯确认不是 continuation summary 内容错误。`ProductionPromptBuilder` 已按模型能力
  投影 history；`AgentRunner` 随后才在 emergency hard-cap request 末尾追加 role=Assistant 的
  `_MAX_STEPS_PROMPT`，这条 late Runtime message 没有 `reasoning_content`。因此 wire 实际为
  `system -> user -> assistant-without-reasoning`，绕过了较早的 Session projection 不变量。
- InfCodeX `openai.ts` 的 load-bearing 设计是在最终 OpenAI wire serializer 对每一条 Assistant
  强制附加 `reasoning_content`，没有内容就发送空字符串；其测试也覆盖 thinking-less、tool-only、
  redacted-only 和 cross-provider history。NZ-Coder 因此把同一 capability invariant 补到
  `prepare_openai_request()` 最终边界，而不是只给 `_MAX_STEPS_PROMPT` 打局部补丁。输入 messages
  会复制后投影，不修改 durable/caller 数据；未声明 replay capability 的 GPT wire 不增加字段。
- 修复后的同 Session 真实 resume 一次成功：trace 为 `continuation_context_projected`、
  `dropped_messages=3`、summary 717 字，`llm_request` 只有 system + 1 条有界 User view；Provider
  一次完成，无 retry/400，执行一次 `read_file README.md` 并读到
  `NZ-CONTINUATION-20260824`。Provider 报告 input 12,559、output 45、reasoning 29、total 12,633；
  该 input 主要包含生产 system prompt 和完整 tool schema，不再包含被边界隐藏的旧 Session prefix。
- 本次显式只有 1 turn，模型选择先读文件，所以 Runtime 诚实返回 `max_turns`，不能把它写成任务
  completed；本次验收目标是 wire 接受、真实工具执行和 resume prefix 隔离。聚焦回归为
  `112 passed`。TP-041 由“大 Session 离线量化 + 小 Session 真实 Provider 数据流”关闭；通用
  长任务效率 TP-025 仍保持 `verify`。
- 最终完整仓库回归为 `2321 passed, 21 skipped, 7 known fork warnings`（146.16 秒）；
  Ruff、compileall 与 `git diff --check` 全绿。

## A270 — Progressive Tool Exposure 成本闭环（2026-08-24）

- A269 的真实单文件只读调用仍报告 12,559 input tokens；同一 trace 的 `llm_request` 却只有
  3,112。离线拆分 63 个实际工具 schema 后得到 10,063 tokens，几乎完整解释差值：历史 prefix
  已被 continuation boundary 隔离，剩余主要成本不是 memory/context，而是每轮全量工具定义。
- NZ-Coder 已存在 `ToolExposurePlanner`、`tool_search` 与 RunContext-owned unlock，但默认策略把
  “1M context 仍宽松”直接等同于“63 个 schema 全部展开”；而 20 个可延迟工具又因默认
  `minimum_deferred_tools=20` 和小 MCP surface 保护回退为全部可见。机制模块存在，但产品默认路径
  没有产生任何成本收益。
- 源码对照 InfCodeX `tool-exposure-planner.ts`、`deferred-tools.ts`、`tool-search.ts` 和
  `tool-resolution.ts` 后保留同一安全边界：read/edit/write/grep/glob/bash/todo/tool_search 等核心工具
  常驻；repo intelligence、workflow 和较少使用的能力由 compact discovery bridge 按 Session
  解锁。没有按 prompt 关键词猜测并删除写工具，也没有改变 registry、handler 或权限接口。
- Planner 现在把 6K schema budget 同时作为调用成本边界：即使 context window 尚未承压，只要
  tool schema 超预算，也启用 progressive exposure；最小延迟规模降为 8，避免真实的 19 个稀有
  工具再次被阈值回退。小型 role 或单个 MCP surface 仍保持直接可见，已解锁工具下一轮恢复完整
  schema。
- 过去只有 buffered/native 路径消费 `expose_specs()`；TUI streaming 直接调用
  `host._active_tool_specs()`，会绕过同一策略。streaming 现在也通过 run-scoped exposure，避免终端
  产品与 headless/SWE 形成成本分叉。
- `llm_request` trace 现在同时记录 message count/tokens、model-visible tool count/schema tokens 和
  合计估算。真实复测的 trace 为 2 messages、3,291 message tokens、46 tools、7,474 schema tokens、
  10,765 total estimate；Provider 实际 input 为 10,190，误差约 5.6%，不再出现 3K 对 12K 的观测
  缺口。
- 同一 Session、相同 1-turn read 模式的真实 A/B：修复前 input 12,559，修复后 10,190，减少
  2,369 tokens（18.9%）；两次均一次 Provider completed、执行一次 `read_file README.md` 并读到
  marker，因显式 1-turn 上限诚实返回 `max_turns`。本地全 catalog 测量为 63/10,063 tokens 降至
  44/7,185，节省 2,878。聚焦回归为 `200 passed`；没有继续购买额外调用追求更激进的 task-specific
  allowlist，避免以能力召回率换一个更漂亮的单题数字。
- 首次全量回归暴露 core-capability benchmark 仍把“低压力全量暴露 100/200 工具”当作成功；口径已
  改为 20/50 小 catalog 全量、100/200 超 6K budget 时只保留核心入口，并新增
  `schema_budget_enforced` 证据。另一项 fork SQLite `disk I/O error` 隔离连续 5 次通过，按既有
  并发 flake 记录且未改 repo intelligence。最终全仓为
  `2324 passed, 21 skipped, 7 known fork warnings`（150.73 秒），静态门禁全绿。

## A271 — Progressive Exposure 召回审计与 InfCodeX Hint 语义纠偏（2026-08-24）

- A270 的 18.9% 单文件成本下降只证明“隐藏 19 个 schema 后核心读取仍可运行”，没有证明模型能在
  workflow、verification、symbol、semantic 等任务里重新找到被隐藏能力。本轮先对真实 63-tool
  catalog 做 8 类自然语言检索矩阵：repo overview、symbol source/callers、changed-file verify、
  workflow execute/history、semantic lookup、project profile。词法索引 8/8 命中，最差目标排名为
  2，说明 `ToolSearchIndex` 本身没有丢召回。
- 真正失败发生在 Agent 可见协议。修复前用 DeepSeek 对
  `exposure-workflow-real-20260824` 做只读 workflow history 查询，模型没有调用 `tool_search`，而是
  绕到 `list_directory -> glob_search -> bash -> read_file`；后两轮又进入 closure reserve，3 次调用
  被拒绝，最终 4 个 Provider calls、6 个工具调用、`max_turns`。所以“搜索 helper 能找到”不能当作
  “模型能发现”。
- 重新逐行核对 InfCodeX `deferred-tools.ts`、`tool-search.ts`、`tool-exposure-planner.ts` 和
  `tool-resolution.ts` 后确认 A270 有语义偏差：InfCodeX 的普通 deferred path 不删除工具，而是保留
  工具名与 parameter schema，只把 rich description 换成明确指向 `tool_search select:NAME` 的 compact
  hint；解锁后恢复完整 description。其 planner 只有在 portable bridge/native-deferred 条件成立时
  才允许把模型可见项降为 bridge/hidden。
- NZ-Coder 的 deferred projection 已改成相同的 callable-hint 语义。19 个延迟工具仍在 Provider
  schema 内，参数结构不变；description 使用项目自有的短用途提示与 exact search 指令；unlock
  继续由 RunContext metadata 隔离，下一轮只对本 Session 恢复完整描述。streaming 与 buffered
  路径继续共用 `expose_specs()`。
- 不能机械复制 InfCodeX 的 hint 长度。NZ-Coder 的原始 workflow 描述只有 53–146 字，第一版长 hint
  反而把 schema 估算从 9,532 增到 9,610 tokens；改为项目适配的短 hint 后为 9,245，首轮约省 3.0%。
  8 类 exact unlock 的两回合 schema + search-result 仍全部为正收益，最差 1.3%、最好 2.6%。该数字
  小于 A270 的 18.9%，但不再用工具不可达换取漂亮的单题成本。
- 相同 workflow 任务在修复后使用新 Session `exposure-workflow-hint-real-20260824`：首轮直接按 compact
  callable schema 调用 `workflow_runs(action=list, limit=50)`，第二轮完成；共 2 个 Provider calls、
  1 个工具调用、0 个 policy block、状态 `completed`，正确报告 0 条持久化 run 且无文件变更。修复前
  的 4-call/6-tool `max_turns` 因而得到真实产品级反证与正证据，而不是只靠 prompt 单测。
- 核心能力 Case D 已固化 8 类 recall、target rank、Session unlock、hint 数量和两回合成本下界；
  100/200-tool catalog 在低上下文压力但超过 schema budget 时仍启用 compact hint，而不是删除工具。
  写文档前相关 Tool Platform、Core Capability、Behavioral Runtime 与 Prompt Builder 回归为
  `46 passed`。最终完整仓库回归为 `2325 passed, 21 skipped, 7 known fork warnings`
  （143.07 秒）；Ruff、compileall 与 `git diff --check` 随后重新执行并通过。

## A272 — Contract-owned 首轮路由与确定性 Subprocess 恢复（2026-08-24）

- 对当前 Runtime 重跑 cron 多文件真实任务后，发现两个“架构已有、数据没有连起来”的问题：
  TaskContract 已持有 5 个精确 artifacts，RepoRetrievalPolicy 却只从原始中文 query 猜路径；
  failure diagnostics 能分类 subprocess cwd 错位，却只说“使用 workspace root”，让模型继续猜相对层级。
- 路由修复保持职责边界：Loop 只在首次 mutation 前取最多 12 个受 TaskContract 校验的
  workspace-relative paths；RetrievalPolicy 自己再拒绝 absolute/`..`、去重并纳入 cache key。
  有 declared artifacts 时路由为 `known-location/read/read_file`，不要求 broad orientation。
  这对应 InfCodeX/infcode-dev 中 planner/session state 是下游 retrieval 的权威输入，而不是各层重新从文本猜测状态。
- cwd 恢复修复也不把 Linux 绝对路径写进代码。Runtime 用 helper 的相对路径深度产生精确
  `Path(__file__).resolve().parents[N]`，同时告诉模型该表达式的实际解析值。
  这与参考项目的“deterministic host/runtime facts 先于 model guess”原则一致，但使用 NZ-Coder 现有
  `RecoveryState -> failure_diagnostics -> synthetic diagnostic` 链路实现，没有添加新框架或旁路。
- 两项修复都先写失败测试：一组锁定三层 helper 必须给 `parents[2]`，另一组锁定
  contract artifacts 必须路由为 known location，并通过 AgentLoop 集成测试证明 paths 真正传到 policy，
  而不是只扩展 helper 签名。受控真实 cwd 烟雾在首次 pytest 失败后只改一行并通过。
- 同一长任务修复后为 17 次 coding calls + 1 次 sidecar、27 次 tools、607,150 total tokens，
  终态 `completed`，exact/independent suite 为 `93 passed`，外部语义矩阵 `9/9 passed`。
  它证明 correctness 链路已恢复，也同时证明 TP-025 仍不能关闭：超过 15/25 的原因是多轮读取、
  一次错误 README 定位和重复 todo 管理，不是 context window 压力。
- 本轮没有为压低一次费用而启用激进 time-based tool-result deletion。InfCodeX
  `microcompaction.ts` 默认关闭并明确说“证据年龄不等于删除后端到端更便宜”；
  infcode-dev 也按 token pressure 与 recent user turns 裁剪。因此下一轮优化应面向“减少不必要回合”，
  而不是破坏当前 run 的证据完整性。
- 最终验证为聚焦 `130 passed`；全仓 `2328 passed, 21 skipped, 7 known fork warnings`
  （142.83 秒）；Ruff、compileall 与 `git diff --check` 全绿。

## A273 — Provider-specific Mutation Shape + Per-run Read State（2026-08-24）

- 源码级对照暴露了“模块都有但 wire shape 不等价”的问题。NZ-Coder 的 canonical
  `apply_patch` 支持多文件，因此 path 位于 `changes[]`；InfCodeX 的 `edit/multi_edit` 把 path
  放在顶层，只让 nested items 表达文本变更。DeepSeek 真实响应连续 3 次漏 nested path，虽然
  Provider 最终看到的 schema 明确保留了 nested `required`。这证明不能把 schema lint 通过当成
  模型实际会遵循复杂 shape。
- `adapt_tool_specs()` 现在只为 DeepSeek family 把 `apply_patch` 投影为单文件批量编辑：顶层
  `required=[path, changes]`，nested items 不再重复 path；handler 原有兼容入口直接消费该 shape。
  GPT/OpenAI 与 canonical catalog 仍保留原多文件定义，适配过程继续 deep-copy，不修改注册表。
  真实同题复测中所有 patch 参数有效，上一轮 3 个 `change 0 requires path` 全部消失。
- 对照 InfCodeX `read-file-state-cache.ts`、`read.ts`、Edit/Write invalidation 和 compaction hook 后，
  NZ-Coder 把 unchanged-read suppression 放进 per-Agent `ToolExecutor`，而不是 files.py 模块全局。
  cache key 是绝对安全路径 + offset + limit，identity 使用 mtime/ctime/size；只记录成功文本读取。
  同路径写入、外部变化、compaction、新 run 均失效，`NZ_READ_DEDUP_ENABLED=0` 是产品 killswitch。
- Runtime TaskContract 已是完整 Requirement ledger，因此 todo 不再与它双写；只有用户明确要求
  todo/checklist 时才恢复 todo tool 与 reminder。这不是删除计划能力，而是消除两个状态 owner。
  同一真实任务从 4 个 todo calls 降到 0。
- 最新真实 Session `tp025-readcache-real-20260824` 为 15 coding + 1 stall sidecar、27 tools、
  512,532 provider tokens、终态 completed。独立 suite `87 passed`、semantic `9/9`；前一可比轮为
  15 + 1、28 tools、683,602 tokens。25.0% token 下降包含模型输出随机性，文档只把“1 次重复全文
  读取被短路、3 次 malformed patch 归零”认作直接因果证据。
- trace 同时暴露新的首要浪费：旧 fixture 源码里的硬编码 cwd 被模型复制成 shell `cd`，policy
  拒绝后又进入 package 目录运行 import，随后才 `pwd`。Recovery 因此新增 workspace-boundary 与
  direct-command package-root 两个精确诊断：给出 active root、要求去掉 `cd/workdir`，并禁止无关
  source/install/environment 探索。该修复已确定性测试，完整付费复测保留到下一次必要产品验收。
- TP-025 仍不关闭：coding calls 已到 15，但 tools=27 尚未达到 25，且单轮 token 下降不是跨任务
  稳定性证据。对齐工作的标准继续是 wire 行为、真实 trace 与独立验收，不是“存在同名模块”。
- 最终全仓验证为 `2341 passed, 21 skipped, 7 known fork warnings`（143.14 秒）；Ruff、
  compileall 与 `git diff --check` 全绿。

## A274 — Sequential Mutation Contract + Existing-semantics Oracle（2026-08-24）

- A273 后的同题失败 trace 证明，剩余两个 patch failure 不是 edit matcher 太严格：一个 batch 的
  第二段 `old_text` 与第一段替换区域重叠，另一个追加测试时猜错精确 anchor。InfCodeX
  `multi_edit` 同样按顺序执行并警告后序 edit 不得重叠，append 场景由独立能力表达；因此没有引入
  可能掩盖错误目标的宽松 fuzzy edit。
- DeepSeek 单文件 `apply_patch` 投影的 description 现在写明 sequential/non-overlapping 约束，并要求
  文件尾新增使用 `op=append`、省略 `old_text`。这只是 Provider wire guidance；canonical catalog、
  GPT/OpenAI 投影和 handler 兼容接口不变。
- 更严重的 correctness 根因是模型为 named form 自行发明语义。Runtime prompt 现在给出通用规则：
  新增 syntax alias 或名称形式前，先 probe 等价的现有 canonical/numeric 形式，以观察结果作为测试
  oracle；保留既有拒绝行为，不发明 range、step 或 scheduler 语义。规则不包含 cron 专有常量。
- 红绿回归首先锁定 DeepSeek description 和 oracle 文本，修复前 2 项失败，修复后与 schema、prompt、
  recovery、hook 组合共 `59 passed`。
- 修复前真实 Session `tp025-boundary-real-20260824` 为 15 coding calls、23 tools、455,435 tokens、
  `max_turns`，产品外验收 `12 failed, 88 passed`；修复后全新 Session
  `tp025-oracle-real-20260824` 为 13 coding + 1 stall sidecar、21 tools、422,892 tokens、
  `completed`。后者所有 6 次 patch 一次成功，产品外 `90 passed`、semantic `10/10` 和真实 CLI
  均通过。
- 因而 TP-025 按原始 15/25 与独立正确性门关闭。剩余成本不再混入该问题：当前首轮仍暴露
  62 tools、约 9.6K schema tokens；是否进一步做动态 tool-set slicing 必须单独设计并证明工具仍可达。
- 最终门禁为聚焦 `59 passed`，相关文件 Ruff、compileall、`git diff --check` 通过；完整仓库
  `2342 passed, 21 skipped, 7 known fork warnings`（142.69 秒）。

## A275 — Task-family Tool Slicing 与 Requirement Ledger 幂等闭环（2026-08-25）

- A274 后仍有 62 个 Provider-visible tools、约 9.6K schema tokens。源码核对 InfCodeX
  `tool-exposure-planner.ts`、`tool-resolution.ts`、`deferred-tools.ts`、`tool-search.ts` 与
  infcode-dev `tool/registry.ts`、Agent/tool blocklist 后，NZ-Coder 不再把“所有稀有能力都提示”当作
  唯一安全策略：核心编码 surface resident；匹配 task family 使用 callable hint；不相关 audited
  family 通过 portable `tool_search` bridge 隐藏；未知/宽泛任务回退旧策略。
- exposure 决策属于 RunContext，不修改全局 registry。隐藏项仍进入 run-local search index，exact
  select 后下一轮恢复完整 schema；trace 记录 visible/deferred/hidden 与 token delta。确定性测试覆盖
  core reachability、workflow intent、unknown fallback、hidden exact unlock 和生产 catalog 6K 门。
  实际离线 catalog 为 63 / 9,532 → 23 / 3,850 tokens；真实 DeepSeek coding 首轮为
  22 visible、39 hidden、9,290 → 3,730。
- 完成前全仓门禁先得到 `1 failed, 2350 passed, 21 skipped`，证明单个已连接 MCP tool 被“不相关
  family”误隐藏。按 InfCodeX portable bridge 边界修正后，小于 8 项的动态 MCP surface 保持 direct
  visible；只有大 surface 才进入 hint/bridge 成本策略。该修复由原有 MCP Runtime 集成测试直接锁定，
  不是只在 planner helper 中自证。
- 第一次真实运行 `a275-tool-slicing-real-20260825` 是必要反证：没有缺失能力，却因一个违背现有排序
  API 的新增测试在 15 calls 后成为 `max_turns`，独立 `1 failed, 87 passed`。trace 还找到三个非模型
  随机问题：pre-edit schema 暴露必被 policy 拒绝的 Bash；非 Git verifier 与 `diff_status` 契约分叉；
  DeepSeek 不理解 exact `old_text` 必须为连续片段。三项均先写失败测试再修复。
- 第二次 `a276-tool-slicing-followup-20260825` 已独立 `88 passed`，但仍诚实返回 `max_turns`。根因是
  exact acceptance 先把 R1–R7 结算，后续只读 `diff_status` 却以同 generation 再调用
  `observe_mutation()`，把 R1–R5 从 satisfied 降回 candidate。Ledger 现在只在真正的新 generation
  撤销旧结论；同 generation changed-path refresh 只补证据、不重开已验收 requirement。
- 第三次全新 59-test fixture `a277-tool-slicing-closure-20260825` 得到 `completed`：15 coding + 1
  stall sidecar、26 tools；内外部 suite 均 `102 passed`，独立语义矩阵 `12/12`。首次 source mutation
  前 Provider schema 从未暴露 Bash，之后才恢复验证能力；无 dispatch/schema/permission failure，
  两次 nonzero pytest 均为真实回归并在同 run 修复。
- 不能把最后的 438,126 total tokens 写成整体成本已经稳定下降。task slicing 的可归因收益是每个早期
  coding call 少约 5.56K schema tokens，并保持隐藏工具可搜索/解锁；总成本仍受 15 轮消息重放、模型
  输出随机性和测试修复次数影响。下一项效率工作应依据多任务 trace 处理 recent-turn/tool-result
  retention，而不是继续无证据缩 resident coding surface。
- 最终门禁为 `2351 passed, 21 skipped, 7 known fork warnings`（145.53 秒）；相关 Ruff、compileall
  与 `git diff --check` 全绿。

## A276 — Provider-only Write Receipt Projection（2026-08-25）

- A277 的逐轮 trace 分解显示，最后一次请求的 durable history 约 33.7K tokens，其中成功
  `apply_patch` 回执本身约占 12.8K；这些回执主要是模型已在上一轮看过的完整 diff。只按 1M
  context window 等到 overflow 才 compaction，不会降低 15 轮任务的二次重放成本。
- 源码核对 InfCodeX `compaction.ts`、`microcompaction.ts`、`file-tracker.ts`、
  `result-extractors.ts` 和 infcode-dev `session/compaction.ts` 后保留其核心约束：microcompaction
  默认不按年龄清空；失败、MCP、控制面和高密度 repo intelligence 证据受保护；语义压缩前先保留
  artifact/file/verification ledger；不删除 tool_call/result 协议对。
- NZ-Coder 没有把参考项目的 pressure-only prune 生搬到 1M context。现有
  `message_projection.py` 已是 durable Session 到 provider wire 的唯一投影层，因此只在这里把
  “已被后续真实 provider assistant 观察过”的成功 file-write 回执换成短 receipt。Runtime 合成的
  verification assistant 不算观察边界；当前工具批次、读取、失败、测试和未知工具结果仍保持完整。
  Provider-facing message 数量、tool_call_id 与 tool result 配对不变。
- 该策略不修改 Session：A279 Session 为 508,164 bytes，7 份完整 write results 共 33,000 chars，
  durable JSON 中短 receipt 数为 0。trace 新增 `context_evidence_projected`，分别记录 acknowledged
  writes、superseded reads/failures 及各自 token savings，避免把既有 stale-read 收益算到新策略上。
- A277 历史离线反事实回放中，最后一轮 history 33,734 → 23,684 tokens（-29.8%），15 轮累计少
  重放约 57,409 tokens。新的真实 A279 反事实为峰值 history 32,537 → 25,009（-23.1%），15 轮
  acknowledged-write 累计少重放约 48,332 tokens；这是相同 Session 前缀的投影 A/B，不受模型输出
  随机性影响。
- 首次 A278 真实运行因用无 TTY 的 `acceptEdits` 启动而把 Bash/verifier 权限询问的 EOF 当拒绝，
  终态 `max_turns`；该轮不能作为能力验收。全新 fixture A279 改用 headless `auto` 权限后为
  15 coding + 1 sidecar、23 tools、7 edits、`completed`，exact acceptance 和产品外 suite 均
  `94 passed`，独立语义矩阵 `12/12`。全程无 DeepSeek replay 400 或 tool protocol failure。
- 最终质量门为 `2355 passed, 21 skipped, 7 known fork warnings`（130.02 秒）；全仓 Ruff、
  `compileall` 与 `git diff --check` 全部通过。

## A277 — 大窗口模型 Replay-Cost Semantic Compaction（2026-08-25）

- A279 在 provider-only write receipt 生效后，后半程 history 仍有 22–25K tokens。逐字段统计显示，
  旧 reasoning 约 10.7K、旧 tool-call arguments 约 5.3K；二者是下一项主要重复成本。由于 DeepSeek
  V4 capability 声明 1M context，原 85% soft/usable hard 阈值在常规长任务中都不会触发。
- 源码审计否决了字段级裁剪。InfCodeX `microcompaction.ts` 明确说明 thinking 不能随意清空，Kimi
  类 provider 会因 tool-call assistant 缺少非空 reasoning 而 400；其 `compaction.ts` 使用 atomic
  blocks 和 rolling semantic summary。infcode-dev `provider/transform.ts` 也会在 replay 时显式保留
  `reasoning_content`，`session/compaction.ts` 保护 recent tail。安全方向是摘要完整旧块，而不是改写
  历史参数或制造孤立 tool result。
- `PromptBudget` 现在同时拥有物理 `usable_input_tokens` 与成本
  `replay_compaction_tokens`。后者默认 24K，可由 `NZ_CONTEXT_REPLAY_COMPACTION_TOKENS` 覆盖，`0`
  完全关闭。Context Runtime 通过独立 `projected_replay_tokens` capability 只估算 provider-visible
  history；system prompt、instructions 和当前 tool catalog 继续只参与物理 request limit，不参与成本
  阈值，因此大型工具 schema 不会让短对话无意义地调用摘要模型。
- replay 超阈值时沿用既有 `auto_compact`：原 transcript 写入 Session artifact，旧头部生成 anchored
  semantic summary，最新 provider-visible suffix 原样保留。marker 新增 `trigger=replay_cost` 且
  `overflow=false`；provider usage/request hard-limit 触发仍分别记录 `provider_usage`/
  `request_estimate` 和真实 overflow。
- 实现中额外发现并修复两个旧压缩边界：只有一个真实 user turn 时，切分器也必须从该 turn 内寻找
  assistant/user 合法 suffix；tail token 估算必须排除 `_nz_*` 与 `_timestamp` 等 durable-only metadata。
  否则 A279 会得到空 tail，或因 Session parts 重复体积把刚完成的写入挤出保护区。
- A279 离线触发点在第 7 次真实模型调用前：history 25,009 tokens；head 15 messages，保留
  diagnostic + write tool call + full result 三条，tail 约 3,821 tokens，summary input 约 18,974。
  真实 DeepSeek V4 Flash 在 14.62 秒生成 4,775-char summary，压缩后 provider history 5,090；随后
  携同一原子 tool pair 请求，prompt usage 5,355、completion 17，返回 `CONTEXT_OK`。
- 用真实 summary 大小代回后续 9 calls，history 累计少重放 142,960 tokens，单轮峰值
  25,009 → 10,490；扣除摘要请求后净收益仍约 12 万 tokens。该反事实只证明 A279 这一条轨迹的成本
  因果，不宣称所有任务或 SWE-bench 分数同步提升。
- 新行为遵循 TDD：先红后绿覆盖 replay-cost、固定 request overhead 不误触发、sync/async parity、
  非 overflow marker、禁用开关、single-human-turn atomic tail、durable metadata 排除和 legacy host
  optional capability。真实 Provider 通过后，最终全量为
  `2363 passed, 21 skipped, 7 known fork warnings`（142.85 秒）；全仓 Ruff、`compileall`、
  `git diff --check` 全部通过。

## A278 — Provider Turn Ledger、证据完成直停与用户约束守卫（2026-08-25）

- A279 虽已正确完成，但 15 个主模型调用都只有 `purpose=coding`，无法区分调查、实现、失败修复、
  验证与收尾。源码核对 InfCodeX 的 iteration/live-turn 一等事件与 infcode-dev 在下次模型调用前检查
  已持久化 terminal facts 的循环后，NZ-Coder 新增 `turn_economy.py`：每轮在 canonical
  `AgentRunner` 入口记录 reason，工具批次结算后再按结构化 tool calls 与 mutation generation 记录
  outcome。记录进入 RuntimeState、有 200 条上限、可中断恢复，并同步进入 trace/headless runtime
  metadata；不靠模型自述推断“做了什么”。
- 原 terminal settler 在第 15 轮前对任何 tool boundary 都无条件继续，即使当前 mutation generation
  已通过用户声明的 exact command 且 ledger 已清空。现在只有 `has_diff + current-generation exact
  acceptance + 无 actionable requirement` 同时成立时，才允许提前进入原 completion review；通过后
  使用 Runtime 确定性摘要直接完成。Review 拒绝后同一 generation 不会反复购买 sidecar 判断，只有
  新 mutation 或真正 natural completion 才能再次审查，原 turn-16 定点修复储备保持不变。
- 第一条真实复测提供了必要反证。用户明确“不修改测试”，但旧 `task_wants_tests()` 看到 pytest 和
  `tests/test_text_utils.py` 就生成 R2“Add or update tests”；Agent 已在第 3 轮通过 4 项验收、第 4 轮
  final，却被 CompletionGate 强制继续，最终第 6 轮新增测试。该次为 7 coding + 1 sidecar、64,201
  tokens，虽然 5 passed，却违反用户约束，因此不计成功样本。
- 根因修复不是增加 prompt 句子：test work 现在只由“add/write/update tests/补充测试”等明确修改意图
  触发，pytest 命令只属于 verification；英文/中文“do not modify tests/不修改测试”进入
  `TaskContract.constraints` 和 RuntimeState。`ProductionToolPolicy` 在 sync/async 唯一分发链上硬阻断
  `write_file/edit_file/apply_patch/write_files_batch` 等对测试路径的写入，源码写入与 pytest 仍可用。
- 同任务在全新 workspace 复测为 3 coding calls、4 tools、22,115 tokens，只改 `text_utils.py`，外部
  `4 passed`；相对错误轨迹 coding calls 7→3、总 Provider calls 8→3、tokens 下降 65.6%。新 trace
  正好是并行读取源码/测试、一次源码写入、一次 exact pytest，第三轮 tool result 后直接 terminal。
- 随后四个全新任务覆盖 query decoding、TTL+LRU、安全路径与三模块 retry queue。五项主模型 calls
  为 3/3/3/5/3（平均 3.4），总 Provider calls 含两个 sidecar 为 19（平均 3.8），总 tokens
  22,115/22,101/28,151/50,726/23,960（平均 29,411）；工具共 26（平均 5.2）。外部独立 pytest
  分别为 4/4、4/4、3/3、12/12、6/6，全部只修改目标源码，exact contract 当前代通过、ledger 无
  unresolved requirement，且都以 `early_tool_completion_candidate=true` 在 tool boundary 完成。
- 这些结果证明简单/中等本地修复不再固定烧到 15 轮，也证明多模块任务能以“批量读取→批量修改→一次
  验收”完成；它们不是 SWE-bench 分数，也不外推为所有模型稳定 3.4 calls。最终门禁为轮次/约束聚焦
  `198 + 108 passed`，完整仓库 `2375 passed, 21 skipped, 7 known fork warnings`（136.40 秒）；
  Ruff、compileall 与 `git diff --check` 全部通过。

## A279 — SWE Trace驱动的Runtime/Repo/Verification收口（2026-08-25）

- A278的20题真实轨迹证明“模块存在”不等于生产路径可用：Matplotlib/Flask冷索引因重名symbol主键
  碰撞失败，targeted test被prompt主动跳过，80轮hard cap前还有隐藏的15轮nominal SLA，公开轨迹又
  使用外层Tracer session。此次以真实失败路径为验收对象，没有继续横向增加新模块。
- Symbol identity现在由各语言analyzer在声明产生时结算：第一次声明保留原稳定ID，后续同名同kind
  声明使用按源码顺序稳定的`duplicate-N` discriminator。这样call/reference在生成时就引用正确ID，
  不需要在数据库写入时事后猜测映射。相同规则覆盖Python AST、Tree-sitter和lexical fallback；真实
  Matplotlib冷索引1,219文件、16,646符号成功，补足了小fixture无法暴露的C++重复类型边界。
- Verification不再把“能编译”误当成“行为正确”。strict SWE组装把`require_targeted`沿
  `execution context → AgentLoop → VerificationManager → planner`传入；planner扩展package-local
  tests发现，但只提升一个最相关候选为required。Scheduler、环境阻塞降级和terminal gate继续复用统一
  生产链，CLI/HTTP的普通任务不因此强制增加pytest成本。
- Work budget沿context-local配置进入canonical `AgentRunner`，普通产品的15轮收敛SLA与SWE的20轮
  诊断SLA分离，二者都受hard cap约束。manifest把nominal budget作为pass@1身份字段，恢复运行时发生
  改变会被拒绝，避免评测口径悄然漂移。
- Benchmark session在Tracer之前显式创建，并同时注入Agent；raw/public trajectory、Session artifact
  与返回metadata因此共享同一`swe-*`身份。strict协议同步声明不可用工具和唯一允许的窄测试路径，减少
  模型通过失败调用才学习本地边界的token浪费。
- TDD先观察manifest、strict protocol、session binding、Python/C++重复符号和targeted verification
  的失败，再做最小实现。最终聚焦组合212项、全仓`2385 passed, 21 skipped`；7条告警均为既有
  Python 3.13 fork弃用提示。未运行付费Provider或官方SWE harness，不能把架构闭环外推成分数提升。

## A280 — 真实续片后的故障语义与首轮热路径收口（2026-08-25）

- 固定顺序第156--175题建立独立诊断续片`lite20-dsv4flash-20260825-a280`，没有重跑A278。
  用户发现余额问题后安全中止：journal保留8个claim、7个result，第8题
  `pylint-dev__pylint-6506`是未结算claim；prediction为7行，raw archive约111 MiB，且没有残留
  `run-agent`进程。该续片不是完整20题，更没有官方Docker harness结果。
- 余额耗尽前只有4题是有效Provider运行：Requests 863生成551字符patch；Xarray 3364和4094分别在
  20轮调查后空patch；Xarray 4248生成1,442字符patch，targeted pytest为18 passed。随后4493、5131、
  Pylint 5859各自把HTTP 402误当可修请求并重复20次，得到的3个`empty_patch`不是Agent能力结果。
- Provider错误边界现在只把400/422和无status的malformed/invalid-request视作可诊断请求；401/402/
  403/404、insufficient balance、payment required、invalid API key和authentication错误直接fatal。
  SWE orchestrator把aborted/error/cancelled/timeout/exception统一记作`agent_failed`，不会再伪装成模型
  生成空patch。该修复不重跑、也不改写已经持久化的pass@1历史。
- Targeted verification新增旧依赖与Python 3.13标准库兼容故障识别：测试收集阶段从`collections`、
  `inspect`、`asyncio`或`typing`导入已删除名字，且没有指向changed file时，记为环境阻塞而不是可继续
  修复的patch失败。普通缺少第三方依赖仍保持repairable，防止过宽降级。
- Closure reserve的`Denied...`属于策略拒绝而不是工具执行错误；因此Xarray 4248这类已有非空diff和
  18项targeted pass的轨迹不会仅因终局预算门被标成patch risk。strict首次写入门在source文件和test
  文件都已定位时从20次提前到12次；测试范围尚未知时仍保留20次，避免用任意读取假装已经定位。
- 完整回归暴露HTTP测试实际选择了默认项目根而不是临时workspace。项目根索引为5,000文件、74,672条
  调用边，原语言路由会在首轮反序列化完整snapshot。测试已按opaque workspace ID选择真实目标；生产
  路由新增SQL distinct语言元数据，并由Repo Intelligence后台状态发布，前台不再等待索引锁或加载
  symbol/call graph。原稳定失败约7--8秒的HTTP流式首包用例降到0.81秒内通过；Repo/HTTP组合144项通过。
- 最终完整仓库门禁为`2393 passed, 21 skipped`，7条告警均是已知Python 3.13多线程进程中`fork`
  的弃用提示；Ruff、compileall和diff whitespace检查同时通过。
- 这里证明的是错误语义、成本止损与首轮延迟边界，不证明patch resolved。剩余最重要的真实问题仍是
  Xarray 3364/4094在20轮内没有形成首次编辑；后续有余额时应只跑新的未尝试题，观察12次局部收敛门
  是否降低空patch，而不是重跑A280美化结果。

## A281 — Compaction任务锚定与Workspace证据完整性（2026-08-25）

- A280的Xarray 3364/4094并非单纯“模型不肯编辑”。逐轮trace显示每次只读调用后都有
  `workspace_patch_created files=1`；Session summary里的所谓diff实际是正在增长的
  `.nz-coder-runs/*.jsonl`。`WorkspaceSnapshotStore`遗漏了这一内部运行目录，而搜索、索引、worktree
  与SWE diff早已排除它。现在快照排除集完成统一，trace增长不再改变snapshot identity，也不会把
  Agent自己的观测日志重放给自己。
- InfCodeX `DefaultSummaryCompaction`的关键契约是只替换旧message entries并保留recent messages；
  infcode-dev进一步用`ContextBudget`、turn-aware tail、2K字符tool输出上限、previous-summary更新和
  single-shot payload recovery构成生产闭环。NZ-Coder原有实现已覆盖这些结构，但`_select_summary_input`
  只逆序选择head，无法保证首条任务进入摘要请求。单一长turn里近期工具证据占满预算时，任务会丢失。
- 选择器现在先预留首个非synthetic human task，再装填最新证据。若首条消息包含显式file expansion且
  整体可放入预算，则仍保留完整内容，维持原payload recovery契约；只有超限时才使用`_nz_user_text`
  作为有界锚点。这是对参考实现“anchored summary”语义的代码级补足，不是新增第二套上下文系统。
- A280还给出一个参考仓库单测未覆盖的Provider边界：DeepSeek在compaction调用没有tools时仍返回
  `<｜｜DSML｜｜tool_calls>`文本。旧代码把任意非空字符串当摘要，压缩后原任务彻底消失。现在空输出、
  DSML/tool-call协议输出会被语义门拒绝；Runtime不重试以避免额外token，而是优先复用合法previous
  summary，否则从首条真实用户任务生成完整固定章节的确定性fallback，并持久化
  `summary_recovery.fallback`供trace审计。
- TDD证据先得到三个预期失败：trace进入manifest、长head请求缺少original task、DSML进入summary；
  修复后相关context/workspace/native组合`68 passed`。最终完整仓库为
  `2395 passed, 21 skipped, 7 known fork warnings`，相关Ruff与compileall通过。本轮完全离线，未恢复
  SWE批次，也不把上下文闭环修复外推成SWE resolved分数。

## A282 — Task Query Authority与工具证据数据流（2026-08-25）

- 三个无上下文审计从不同入口得到同一结论：参考架构中compaction/summary是派生continuity state，
  不能取代用户任务authority。NZ-Coder虽然已经把原任务持久化到`RuntimeState.initial_task_text`，但
  `ProductionPromptBuilder`的memory、implementation bundle和repo retrieval仍分别调用
  `_last_user_text(messages)`；带`_nz_compaction`的User-role summary因此同时污染三条检索链。
- 修复没有增加一个新的“记忆模块”，而是统一现有authority：每轮只解析一次`task_query`，跳过
  synthetic、compaction及legacy`<session-summary>`，优先真正的后续用户指令，否则回退initial task，
  最后统一截断到300字符。三个动态上下文消费者共享同一值，避免同轮语义分叉。
- 同一规则进入unfinished-run continuation builder。过去max-turn发生在compaction后时，boundary会把
  summary wrapper写入`## Latest User Instruction`；现在它回退canonical task，恢复时仍由真正的新
  user follow-up拥有最高authority。
- A280的4094还暴露了状态证据断链：精确测试文件的成功content grep能证明该文件已被检查，但
  `observe_tool()`只保存pattern。现在只有“单文件路径 + content模式 + 非空成功匹配”才进入
  `read_files`；目录泛搜、No files found、Error/Denied和失败调用均不计。这样直接复用现有
  `pre_edit_scope_localized()`与12次strict limit，而不是再叠加一个模型sidecar。
- 这轮学习的重点是：源码级对齐不能只看是否存在compaction、memory或stall detector模块，必须沿
  `消息authority → query消费者`以及`工具结果 → RuntimeState → admission policy`检查证据是否真的
  到达决策点。InfCodeX与infcode-dev的exact-input doom-loop检测也不能识别不断变化pattern的语义漂移，
  因此把已有客观定位证据接入确定性状态机比复制另一个重复检测器更可靠、更省token。
- 四个RED测试分别锁定三路query回退、continuation authority、精确grep定位和负例边界；聚焦组合
  `173 passed`，全仓`2399 passed, 21 skipped, 7 known fork warnings`。Ruff、compileall和diff检查
  均通过；未调用Provider或官方SWE harness，不宣称分数提升。

## A283 — Terminal metadata必须到达SessionProcessor（2026-08-25）

- A282接通`grep result → RuntimeState → strict policy`后，继续向下追踪发现最后一段仍断开：policy在
  第二次strict违规时已产生`strict_terminal_blocker=true`，result projector却只消费
  `stall_kind=consecutive`。因此trace里的“terminal”只是名称，processor仍可能返回continue。
- `ProductionToolResultProjector`现在集中判定terminal denial；consecutive doom loop与
  strict terminal blocker都会令batch `blocked=true`，并以`continue_on_deny=false`结算tool part。
  第一次strict反馈和普通permission denial维持可恢复语义，避免把一次无害拒绝升级成整次run失败。
- 端到端测试没有依赖mock状态字段：真实读取`app.py`与`tests/test_app.py`，在首批累积12次调查，再用
  两个不同grep参数确认exact-repeat detector不会介入。最终状态为blocked且Provider calls精确等于3，
  第四个响应未消费，证明SessionProcessor真正收到了终止事实。
- 全量回归反过来发现LSP错误报告的旧竞态。stdout EOF线程可能在stderr线程写入deque前结算pending
  initialize request，缓存错误因而丢掉server stderr。初始化异常清理现在有界join stderr reader，测试
  人为延迟reader 50ms锁定该时序；等待上限仅0.2秒，只影响启动失败路径。
- 这一轮说明源码级对齐必须一直跟到终端消费者：`guardrail metadata`存在并不等于lifecycle已经执行，
  `stderr pipe`存在也不等于诊断已经排空。最终全仓`2401 passed, 21 skipped, 7 known fork warnings`，
  静态门禁通过；没有付费Provider或官方SWE评测证据，不外推分数。

## A284 — Terminal State必须保持语义类型（2026-08-26）

- A283只证明raw lifecycle能够返回`blocked`。继续沿native consumer审计发现，Runner显式把它映射为
  `RunStatus.ERROR`，而Durable Session枚举也无法表示blocked。模块存在、raw JSON正确，都不代表产品
  客户端看见了正确终态。
- 参考架构保留了“被策略停止”和“运行故障”的区别：InfCodeX通过hook stop reason保留来源，
  infcode-dev/OpenCode在session context中保留blocked事实。NZ-Coder采用与现有typed contract一致的
  最小做法，为Run与Session各增加同名状态，不另建一套停止协议。
- 行为测试必须越过private mapper：让native lifecycle返回blocked，并同时断言`RunResult`、
  `RunContext`和session finalizer；另用真实`SessionRuntime`确认磁盘快照可表达该状态。

## A285 — Repository Scope不是File Evidence（2026-08-26）

- `read_file(path="tests/runtime")`成功并不表示任何测试文件内容被读取；`repo_map(path="src")`只证明
  浏览了结构；`read_symbol`返回available-symbol列表也不证明目标声明已定位。旧状态机把这些path统一
  填入`read_files`，会让确定性收敛策略建立在错误前提上。
- 修复边界放在`tool result → RuntimeState`适配处，而不是给`pre_edit_scope_localized()`增加更多路径
  猜测。`read_file`消费自身`<type>file</type>`协议，`read_symbol`排除not-found，repo/ref scope完全不
  进入exact-read ledger。这样下游closure paths与strict policy自动获得同一份可信证据。
- 这条经验适用于继续对齐InfCodeX/infcode-dev：Repo Intelligence的能力不取决于工具数量，而取决于
  每类结果在进入状态机时是否带有可验证语义。导航信号、定位证据和修改证据不能共用一个模糊path列表。

## A286 — Compaction只能是Context，不能成为Task Authority（2026-08-26）

- A282修过PromptBuilder和continuation，但lifecycle还是独立解析最新User消息。该重复selector位于
  `RuntimeState.initial_task_text`的生产入口，因此summary污染会重新传播到task mode、acceptance、
  declared test scope和planner；只修下游query consumer不够。
- lifecycle现在复用相同语义边界：跳过synthetic、`_nz_compaction`和legacy session-summary。真正的新
  用户follow-up仍优先，更早真实任务可回退，纯派生消息不能创建canonical task。
- 本轮三项RED分别锁定typed terminal、exact read evidence和canonical producer。相关组合
  `116 passed`，全仓`2407 passed, 21 skipped, 7 known fork warnings`，静态门禁全绿。这里的对齐含义
  是“同一事实穿透生产者与最终消费者”，不是简单拥有同名模块；未运行Provider或SWE harness。

## A287 — Resume是新Activation，不是新Task（2026-08-26）

- 离线生产复现得到明确事实：max-turn收口后的inactive RuntimeState返回`loaded=false`；随后
  `initial_task_text`变为`go on`，PromptBuilder三条task-aware query也全部是`go on`。Continuation summary
  正确存在并不足以修复这两个authority消费者。
- InfCodeX的managed role prompt同时呈现`Original user request`和仅在不同文本时出现的
  `Current round instructions`；infcode-dev用synthetic compaction-continue part承载自动续跑。共同原则是
  continuation可以触发新一轮执行，但不能获得original-task authority。
- NZ-Coder将boundary识别、纯继续判断和task recovery收口到`continuation_context`。Lifecycle负责恢复
  task state，PromptBuilder负责task-aware retrieval，Tool Exposure负责schema family，但三者消费同一
  resolver。绿色重构删除了PromptBuilder旧的重复selector。
- RuntimeState的`active`现在表示“任务是否仍可恢复”，而不是“Python调用栈是否仍在运行”。max-turn和
  interrupted保持active；completed/error/blocked仍关闭。新activation重置turn/time与budget phase，
  保留contract、ledger和证据，避免既丢任务又复用耗尽预算的两种错误极端。

## A288 — 收敛依据应是可信Evidence，而不是配置字段存在（2026-08-26）

- 旧门把`verification_contract.command`当作唯一收敛资格，实际等价于“用户写了测试命令才控制成本”。
  SWE题目经常只描述行为和目标文件，Xarray 3364/4094正属于没有显式contract但已有精确定位的情况。
- A285先解决evidence quality，本轮才安全扩大consumer：只有协议证明读过精确源码和测试文件时，
  `pre_edit_scope_localized()`才能替代contract；目录、scope和未命中仍被排除。顺序不能反过来，否则效率
  门会把错误证据放大成错误阻断。
- Model-facing schema删除与execution-time policy必须同时生效。前者降低下一轮schema/token并引导模型，
  后者处理模型从历史记住旧工具的情况；只做任一层都会留下可绕过路径。所有write/edit工具保持可用，
  所以这是convergence gate而不是run termination。
- A287/A288合计七个测试均完成RED→GREEN，相关组合`202 passed`，全仓
  `2414 passed, 21 skipped, 7 known fork warnings`。本轮完全离线，不把确定性控制流修复外推成
  SWE-bench分数或真实Provider token节省比例。

## A289 — Original Task与Current Round Instruction必须分层（2026-08-26）

- A287只覆盖纯继续消息。对`Continue ... Do not modify tests. Run pytest ...`做真实Lifecycle复现后，
  新指令在restore之前解析正确，但随后被旧快照的`verification_contract`、`forbids_test_changes`和
  `requested_paths`逐字段覆盖。说明“能恢复原任务”与“能接收本轮约束”是两条独立的数据流。
- InfCodeX `role-prompt.ts`保留`originalTask`作为contract objective，并在prompt不同时额外呈现
  `Current round instructions`；后者没有替换前者，但拥有当前执行轮的指令权威。NZ-Coder现在把同一
  区分落实到Lifecycle参数，而不是靠模型从混合summary里猜优先级。
- 恢复时保持`initial_task_text`、TaskContract、RequirementLedger、workspace mutation和read evidence；
  对当前轮只合并可确定提取的criteria/path，并让最新显式测试修改约束与pytest verification contract
  覆盖旧策略事实。新命令从attempts=0的合同开始，避免把旧命令的pass错误迁移到新命令。
- 最新User约束按同级authority处理冲突：`do not modify tests`会关闭测试修改；后续明确`add tests`又能
  解除旧限制。没有显式相关语义时保持旧值，纯`go on`不触碰任何合同事实。
- 两项RED分别锁定生产Lifecycle的旧命令覆盖和相反测试约束更新；相关组合`163 passed`，全仓
  `2416 passed, 21 skipped, 7 known fork warnings`，静态门禁通过。没有付费Provider或官方SWE评测，
  因而这里只声明源码数据流对齐，不声明分数等价。

## A290 — Current Round也必须进入确定性Completion Authority（2026-08-26）

- A289之后，消息与运行策略已经同时看到follow-up，但`TaskContract/RequirementLedger`仍是旧快照。
  当旧items全部satisfied时，新增docs交付与新pytest命令不会阻止`CompletionGate.ready=true`。这是
  prompt authority和deterministic authority分叉，不是模型遵循度问题。
- InfCodeX让当前worker/evaluator同时看到`originalTask`与round instruction；NZ-Coder的completion gate
  是不读消息的本地判定器，因此采用等价的显式投影：原objective不变，旧ledger evidence按ID保留，
  本轮带精确acceptance command的新增requirements以pending加入，新verification替换旧命令证据。
- 合并只消费现有`derive_task_contract`可验证的结构，不新增planner调用。没有精确pytest命令时仍不把
  自然语言推断升级成不可满足的硬gate，维持原来的保守边界。
- 直接复用初始Repo artifact resolution一度产生假目标：`update docs/parser.md`会命中仓库中无关
  `update.py/update.ts`。第二个RED把这一点稳定复现；round contract现在只接受当前User文本显式路径，
  防止检索候选变成completion硬要求。
- 相关组合`189 passed`，全仓`2417 passed, 21 skipped, 7 known fork warnings`，Ruff、compileall与
  whitespace门禁通过。没有Provider或官方SWE运行，不把控制流修复外推成成绩。

## A291 — 没有精确测试命令时，Hard Gate只能承诺可观测事实（2026-08-26）

- A290保守地拒绝把无命令自然语言变成硬合同，但这也让`update docs/parser.md`只停留在prompt层；旧
  ledger已经satisfied时，本地CompletionGate无需读取消息便会直接放行。避免误判与避免漏判必须拆成
  两种authority，不能简单选择其中一个。
- 新的artifact-only round contract只消费同一句中的明确修改动词和明确路径。`docs/artifact`使用
  deterministic模式，由成功write/edit证据满足；行为描述仍交给Sidecar语义审查。纯读取、解释以及
  被`do not/without/不要修改`否定的路径不升级为交付物，正向路径位于否定短语之前时仍被保留。
- `merge_round_task_contract`现在也接受没有verification requirement的增量合同：保留原objective、旧
  acceptance command和verification item，只追加去重后的artifact requirement；初始合同为空时使用
  original task建立最小artifact合同。这使重复resume幂等，也不虚构新的测试证据。
- `current_round_instruction_text`作为RuntimeState中的独立有界字段持久化，并进入Sidecar真实请求的
  additional criteria。原任务仍是canonical objective，当前轮指令只拥有本轮执行/完成审查authority，
  与InfCodeX同时展示Original Task和Current Round Instructions的分层语义一致。
- 相关组合`218 passed`，全仓`2421 passed, 21 skipped, 7 known fork warnings`；Ruff、compileall均
  通过。全程离线，未运行Provider、SWE实例或Docker harness。

## A292 — Evidence失效必须跟随Mutation Scope，而不是任意写入计数（2026-08-26）

- A291新增docs artifact后暴露了旧generation模型的过度失效：任意文件写入都会推进全局generation，
  ledger、exact acceptance和Sidecar遂同时认定旧源码证据过期。结果虽然安全，但会让“代码已验证后补
  文档”重复跑pytest、重复调用Verifier，形成确定性的token与工具浪费。
- InfCodeX没有相同的Python RequirementLedger可直接复制，但其源码给出明确边界：
  `ManagedMutationTracker`保留逐路径mutation，Verifier Context消费真实file-edit summary，docs-only
  policy按路径限制写入。对齐的重点因此是保留mutation attribution，而不是照搬一个全局计数器。
- NZ-Coder保留`mutation_generation`作为全部工作区变化/Sidecar review代际，新增
  `acceptance_mutation_generation`表示exact acceptance有效性。文档路径不会推进后者；source、test、
  config、mixed和pathless写入会推进。首次docs-only任务仍在generation 0执行一次用户声明验收，之后
  继续补文档不会重复执行。
- RequirementLedger的`latest_generation`同步改为最后一次会使行为证据失效的代际。docs requirement
  仍记录其真实mutation generation并由写入满足；已验证behavior/verification保持satisfied。Semantic
  review也绑定acceptance generation，因此在源码验证后补文档不会让兼容性证据陷入永远candidate。
- 兼容性边界采用显式未初始化值：旧快照迁移时保守使用原全局generation；未经过reset的手工/native
  state也回退旧语义；正常activation从0初始化scoped generation。未知路径写入绝不视作docs-only。
- 系统化调试沿`write result → RuntimeState → RequirementLedger → exact contract → Sidecar → native
  terminal`逐层复现，TDD先获得七类预期失败再实现。相关组合`183 passed`，全仓
  `2434 passed, 21 skipped, 7 known fork warnings`；全程离线，未运行Provider或SWE harness。

## A293 — Tool Execution Mode不等于Workspace Side Effect（2026-08-26）

- 源码枚举先排除了“照着名字抄”的错误方向：NZ-Coder没有注册`delete_file`、`rename_file`、
  `multi_edit`或`insert_after_anchor`，但旧`turn_economy`却保留这些InfCodeX名字；与此同时，真实的
  `write_files_batch`和`apply_agent_changes`没有进入该分类。模块对齐不能用参考仓库的tool name替代
  本项目注册表中的真实effect。
- 根因是NZ注册器只有调度维度`read/serial/write`。`apply_agent_changes`与`workflow_save`同为
  `execution=write`，前者改任务workspace，后者只改产品私有状态；MCP write又可能修改远端。一个布尔
  write无法同时服务事务、权限、验收失效、Provider-turn归因和Verifier规模判断。
- InfCodeX `tools/side-effect.ts`把dominant effect声明为`readonly/reads-network/mutates-fs/
  mutates-shell/mutates-network/mutates-state`，`registry.ts::isToolFileMutation()`从注册metadata派生；
  managed wrapper只因循环依赖保留一份`MUTATES_FS_TOOL_NAMES`，并用registry-parity test防漂移。
  NZ-Coder采用同一边界：注册器保存六类effect，保留一份cycle-safe核心filesystem catalog并以真实注册
  parity测试约束，所有决策消费者调用统一predicate。
- 统一`collect_filesystem_mutation_paths()`递归消费path-shaped fields，覆盖`path/file_path`、
  batch/patch item及child merge的`reviewed_files`。无法得到路径不是“没有修改”，而是unattributed
  mutation：RequirementLedger和acceptance保守失效；明确全部为文档时才沿用A292的scoped evidence。
- 这条数据流已贯穿RuntimeState、TurnEconomy、RunEvidence、Admission invariant、child postcondition、
  ToolExecutor read cache、Loop code-index/LSP refresh与SWE trace generation。`apply_patch(dry_run=true)`
  被明确排除；内部workflow persistence标为`mutates-state`；非事务MCP write标为
  `mutates-network`，不会伪造本地diff。
- InfCodeX的`ManagedMutationTracker.riskyShellOps/unattributedWriteOps`提示了另一个盲区：Bash命令即使
  无法提取文件也可能已改变workspace。NZ现在以现有command policy为唯一classifier，mutating shell
  作为pathless mutation推进generation并清除旧verification；read/test命令不推进。非零命令也可能
  部分写入，因此只要已执行且被策略判为mutating就保守记录。
- RED测试顺带证明旧exact-test heuristic把`touch src/generated.py`和`cat src/app.py`当成测试，因为它
  只搜索`.py`参数。修复后必须先出现真实test runner，再判断文件/符号filter；这避免mutation命令错误
  增加verification计数。相关完整组合`342 passed`，最终全仓
  `2448 passed, 21 skipped, 7 known fork warnings`；Ruff、compileall和diff检查通过。未调用Provider或
  SWE harness，不把控制流正确性外推成resolved率。

## A294 — Side Effect之外还需要Plan-mode Override（2026-08-26）

- A293只完成了effect“生产”，并未证明所有授权消费者已经迁移。源码审计发现
  `permissioning/checker.py`和read-only subagent仍读取`execution=read/write/serial`。这个字段只描述
  scheduler能否并发、是否需要串行，不能回答用户是否批准本地编辑、远端写或内部状态迁移。
- InfCodeX `tools/types.ts`把`sideEffect`设为必需字段，同时保留独立`planModeAllowed?: boolean`；
  `registry.ts::isToolPlanModeAllowed()`规则是：显式true优先、显式false禁止、否则仅readonly允许、未知
  fail-closed。NZ采用同一语义，而没有把question/todo/plan_exit等规划环状态写错误重标成readonly。
- 新的`get_tool_policy_snapshot()`一次性投影active builtin与dynamic tool的`side_effect`和最终
  `plan_mode_allowed`。Plan schema exposure、permission checker与read-only child都消费注册metadata；
  compatibility `READ_TOOLS/WRITE_TOOLS`仍可供旧扩展import，但不再决定权限。动态MCP定义也进入同一
  snapshot，read与remote mutation不再靠`mcp_`名字或execution猜测。
- `acceptEdits`语义被收窄为“自动接受本地文件编辑”，不是“自动接受任何execution=write”。安全的
  session-state工具保留显式小型例外；其余mutates-state/mutates-shell/mutates-network在default与
  acceptEdits下仍需批准，auto模式才全自动。Plan则在permission和model-facing schema两层执行同一门，
  防止历史schema重放与下一轮无效调用。
- read-only child只按`readonly/reads-network`自动开放；Bash因已有强制read-only command classifier而
  保留，skill/optional loader与project-profile cache是明确的私有状态例外。write child不受该筛选。
  这比不断维护`_SUBAGENT_READ_ONLY_BLOCKED_TOOLS`更能覆盖以后新增的插件工具。
- 全量回归暴露了两个重要迁移经验：声明式`emit_handoff`虽然是mutates-state，但其authority已被
  AgentGraph限制，属于显式安全状态操作；optional LSP即使尚未import，permission仍需读取optional-pack
  声明的read effect。两者都通过补全metadata链解决，没有恢复“unknown默认allow”。最终全仓
`2453 passed, 21 skipped, 7 known fork warnings`，静态门禁通过；未调用Provider或SWE harness。

## A295–A299 — Tool Policy从声明到执行闭环（2026-08-26）

- AgentGraph admission不再维护另一份工具名分类：builtin、optional和MCP工具统一从注册器的
  `side_effect/plan_mode_allowed`投影read、network、shell、filesystem和state capability；交互与子Agent
  只保留窄的显式语义例外。
- `session.tool.*`事件与ToolExecutor共享同一个category投影，授权时解析出的动态工具代际会随
  `ToolExecutionResult`保存，不会在完成事件阶段重新查询已经热更新的MCP定义。
- optional pack新增单一owner、幂等同定义和active registration优先级；动态工具不能占用尚未加载的
  optional名称。权限规则改为显式ask先于安全状态默认值，Bash/process也不能用宽泛allow绕过命令级
  安全检查或Plan Mode。
- 这些修改对齐的是InfCodeX把调度、side effect、Plan authority和事件语义分开的源码边界；不是复制
  参考仓库的工具名表。

## A300–A310 — Provider/Tool-call边界与上下文一致性（2026-08-26）

- 每个tool batch使用`scoped_dynamic_tool_snapshot()`冻结一代MCP目录，permission、scheduler、handler
  和结果事件不会发生TOCTOU；postponed annotation和bool/int等JSON标量按真实handler签名校验。
- Provider返回非object arguments、缺失function/name、重复/缺失/超长call id，乃至`tool_calls`数组中的
  `null`时，都会形成合法可配对的assistant/tool历史和模型可见修复诊断，不再在SessionProcessor或权限
  层抛`KeyError/AttributeError`。所有修复均记录trace且不修改原Provider对象。
- CJK token估算改用非ASCII原文而非`\uXXXX`膨胀；大型tool output用内容hash避免同ID覆盖；同workspace
  的HTTP/CLI Session共享MemoryManager与mutation lock。Shell拒绝command/process substitution、backtick、
  独立`&`和换行逃逸，workspace/home/symlink与大型输入边界同步收紧。
- TraceRecorder对循环、深层、超大容器和坏`__str__`做有界降级，观测失败不再终止Agent。

## A311–A322 — 生命周期、Session identity与并发所有权（2026-08-26）

- process start先执行shell/workspace/Plan硬门，再应用用户allow/ask；BackgroundAgentManager拥有
  closing/closed状态和可重试close，关闭一个Session不会持有全局registry lock阻塞其他workspace。
- AgentLoop只有在child manager成功settle后才释放repo/event/MCP/tracer资源；随后按精确manager identity
  从workspace registry移除，重新打开同一Session会得到新的live manager。
- `ensure/save/rename/activate/scoped/delete`要求精确、非保留Session ID；`active/latest`只作为只读alias，
  损坏alias不再被lossy sanitization映射到另一个Session。Repo intelligence acquire的lookup/create/refcount
  在一把锁内完成，消除了最终release与新acquire之间的KeyError竞态。
- legacy streaming watchdog复用canonical gateway timeout iterator，timeout/cancel会关闭上游stream；Event
  replay和Trace summary的`limit=0`不再因Python `[-0:]`返回全部历史；Session枚举可容忍并发删除。

## A323–A336 — 长任务持久化与诊断抗损坏（2026-08-26）

- Trace轮转在`glob()`与`stat()`之间删除文件时会跳过失效候选；summary忽略标量JSON行，并安全解析损坏
  duration/ts/token/attempts。Event journal是明确的best-effort边界：循环或无法deepcopy的插件metadata只
  降级该记录，不会打断live Agent fan-out。
- 静态工具注册增加一致性锁；optional pack导入以事务快照提交，import失败不会留下半套handler/schema/
  effect。`tool_search`把exposure导入延迟到调用期，消除了直接导入progressive exposure时的循环依赖。
- Session JSON即使语法合法但根节点不是object也按损坏处理。Git tracked instruction缓存现在绑定worktree
  index代际、清理同文件旧键并限制2048项，`git add/rm`后authority label立即更新且不再无界增长。
- Markdown Memory记录、索引、auto-dream报告和状态改为`fsync + atomic replace`；提交点失败保留上一版本
  并清理临时文件。损坏assistant timestamp不会让micro-compaction崩溃。
- ChildAgentResult忽略NaN/Infinity usage和伪布尔字段；Workflow终态持久化处理短写、提交失败清理、损坏
  ended_at排序和非有限cost metrics，因此旧/手工状态不能阻止child settle或历史查看。
- 本轮每项生产修复都先用确定性RED复现。首次全仓回归为
  `2534 passed, 21 skipped, 2 failed, 7 known fork warnings`；两项失败是动态工具snapshot wrapper引入后
  源码架构守卫仍扫描旧函数体，守卫现同时验证public wrapper与snapshot implementation（相关
  `34 passed`）。修复后第二次全仓为`2541 passed, 21 skipped, 7 known fork warnings`，Ruff、compileall
  与diff检查通过；全程未调用付费Provider、SWE实例或Docker harness。

## A337–A341 — 最后一轮API零值、Session提交与Schema隔离（2026-08-26）

- Workflow store与`workflow_runs`工具过去都把`limit=0/-1`强制成1，和已经修复的Session/Event/Trace
  API语义不一致；现在底层与active+persistent合并层都返回空列表，不会意外暴露一条历史记录。
- `activate_session()`原先先设置ContextVar再写active alias；磁盘失败会留下仅进程内可见的幽灵Session。
  提交顺序现改为先完成原子持久化，再发布process-local identity。
- `get_catalog_specs()`原先只复制list，嵌套schema仍指向全局对象；一个Provider adapter修改description或
  properties会污染其他Session。现在builtin/dynamic schema均深拷贝，且register入口验证参数schema为
  有限、JSON可序列化对象，非法set/NaN/循环结构在暴露前失败且不产生部分注册。
- exposure-first与runtime-first分别在全新Python进程导入成功；最终全仓为
  `2546 passed, 21 skipped, 7 known fork warnings`，Ruff、compileall与diff whitespace门禁通过。仍未调用
  Provider、SWE实例或Docker harness。已安装真实入口在`/home/pyh/test_nzcoder`运行`nz-coder --help`、
  `doctor --help`、secret-free `doctor --json`、`config show`和`models list`均为exit 0；doctor确认Python、
  workspace/state security、三类parser、watcher、LSP、model配置与credential presence通过，仅对未配置的
  experimental semantic provider和非交互终端给出预期warning。

## A342 — Repo Map必须排除产品临时副本并补偿Watcher启动窗口（2026-08-26）

> 2026-08-28 范围审计纠偏：`.product-*` 是手工产品测试产生的临时目录，不是 NZ-Coder 管理目录，
> 也没有 InfCodeX 保留字依据。A342 的“所有未知 dot-directory”剪枝、A343 的 `.product-*` 特判及
> A345 的 snapshot 特判已回退；用户拥有的 `.product-*`、`.ci-tools` 等隐藏源码重新可见。Watcher
> 启动窗口补偿与 `.nz-coder`、`.nz-coder-runs`、缓存、依赖和构建目录排除仍保留。

- 在`/home/pyh/test_nzcoder`走真实`dispatch("repo_map")`发现，未知`.product-*`目录不在静态exclude表中，
  旧实现扫描80个临时副本文件并报告另有316个被额度截断，真实`cron_engine/taskr`几乎没有进入上下文。
  这会直接放大token、误导搜索并让Agent修改过期副本。
- PersistentCodeIndex现在默认剪枝所有未知dot-directory（显式扫描某个hidden base仍可用），native watcher
  的event eligibility消费同一predicate，避免初始索引和增量索引出现不同目录语义。
- 组合回归暴露`start_watching()`返回与OS watch真正安装之间可丢失立即创建事件；native watcher启动时
  先比较传入known fingerprint与当前快照并增量补偿。相关组合`22 passed`，启动竞态测试另连续通过两次。
- 真实refresh后repo map从`80 scanned / 316 omitted / only python temporary copies`变为
  `24 scanned / bash+cpp+python`，首个结果为真实`cron_engine/cli.py`。最终全仓仍为
  `2546 passed, 21 skipped, 7 known fork warnings`，静态门禁通过；没有Provider/SWE/Docker运行。

## A343 — Repo Intelligence入口统一排除产品运行时副本（2026-08-26）

- A342只收口了持久索引；确定性RED证明`smart_search`的非Git回退仍会返回
  `.product-stale/stale.py`，ProjectProfile语言采样也会把临时副本中的Go误报为当前项目语言。
- `smart_search`、`find_symbol_callers`、变更文件过滤，以及ProjectProfile的语言采样、source-root、
  Python package和single nested project探测现统一排除`.product-*`目录；规则没有扩展到`.github`
  等合法隐藏源码目录。
- 两项RED转绿后最终全仓为`2548 passed, 21 skipped, 7 known fork warnings in 132.46s`；Ruff、
  compileall和diff whitespace门禁通过。真实`/home/pyh/test_nzcoder`调用`smart_search`返回当前
  `cron_engine/parser.py`等源码且不含`.product-*`。全程没有Provider、SWE实例或Docker运行。

## A344 — SWE超时隔离从隐式Fork迁移到显式Spawn（2026-08-26）

- 根因不是单一warning：SWE单题执行器硬编码`fork`，依赖它隐式继承workspace/ContextVar并绕过
  `TraceRecorder`线程锁的pickle边界；在Python 3.13多线程父进程中存在真实死锁风险。
- Agent timeout与Docker pull timeout现统一使用显式`spawn`。父进程把workspace、全部runtime overrides、
  broad-test guard和declared test scopes作为可序列化快照传入；TraceRecorder只序列化durable identity，
  子进程重建本地锁。Windows也不再因缺少`os.fork`而退化为无硬超时。
- 两轮确定性RED分别证明旧实现请求`fork`、tracer无法pickle、无`os.fork`时绕过process timeout，以及
  Docker worker使用平台默认context。聚焦`76 passed`；全仓为
  `2552 passed, 21 skipped, 1 intentional fork-compat warning in 193.06s`。唯一warning来自明确验证
  `register_at_fork`清理的兼容测试，现只在该测试调用边界局部过滤，生产路径不吞警告。

## A345 — 真实终端任务收口Tool Schema与Workspace Snapshot（2026-08-26）

- 在`/home/pyh/test_nzcoder`用已安装的`nz-coder run`执行同一条只读代码理解任务，首次真实trace显示
  5次Provider请求均携带约5908 token的41项完整工具schema，schema累计29540 token；根因是“小于
  6000 token”单一门槛把41/42项目录错误视为可一次性暴露。对照InfCodeX deferred tools的紧凑提示
  设计，progressive exposure新增32项eager上限，并把`control flow/控制流/代码流程`纳入repo
  intelligence意图。修复后同任务schema累计降至15635 token，下降47.1%；总usage从73071降至
  63737，下降12.8%，同时保留Repo Map、文件读取、grep/glob等任务相关能力。
- 同一真实运行还发现workspace snapshot把历史`.product-*`副本作为用户交付物复制，artifact从约
  2.9 MiB膨胀。Snapshot现只新增明确的产品临时目录排除规则，不泛化排除`.github`等合法隐藏源码；
  重跑后`.product-*`计数为0，snapshot约524 KiB。
- 真实写任务在独立fixture中修复`normalize_query`的Unicode空白折叠与casefold：4个coding turn、5次
  工具调用完成read→search→edit→精确pytest闭环，fresh manual verification为`2 passed in 0.00s`，
  仅`query.py`发生最小修改；无工具失败、重试、压缩或重复搜索。该运行证明native write、权限、
  patch、精确验证与Sidecar在真实Provider路径闭合，不只依赖mock测试。

## A346 — Resumed Session的Grounded History Report不再重复调用Sidecar（2026-08-26）

- 对上述真实Session执行“禁止工具和文件修改，只复述上一轮函数与测试结果”的续聊时，旧InfCodeX式
  `default-fire`仍额外调用Verifier；本轮没有任何新工作，Sidecar却消耗1873 token，总usage为11277。
  根因是gate只计算最后一条真实User消息之后的工具证据，忽略Session历史中已经持久化的edit与pytest。
- 新规则没有放宽“文本声称完成但无证据必须复核”的安全底线。只有同时满足以下条件才跳过：历史中
  确有assistant tool call、当前明确禁止调用工具、明确禁止修改文件、请求report/summary/复述历史结果、
  且没有新的action imperative。无历史证据和`implement endpoint`等新动作仍走`default-fire`。
- 同一Session、同一提示真实重跑后，trace明确记录
  `sidecar_gate_decision fire=false reason=grounded-history-report`：1次coding请求、0工具、0 Sidecar、
  0文件变化，正确返回上一轮`2 passed in 0.00s`证据；总usage为9490，较修复前下降15.8%。
- 最终门禁：`2554 passed, 21 skipped in 199.93s`，无warning；全仓Ruff、compileall及本轮文件
  `git diff --check`全部通过。本轮运行了三个小型真实Provider任务，没有启动SWE实例、Docker harness
  或恢复批量评测，因此不外推SWE resolved率。

## A347–A348 — Lite开发轨迹驱动的验证收敛与仓库缓存（2026-08-27）

- A347是12题开发观察窗口，不是官方Docker harness成绩：2项内部`completed`、7项`risky`、1项
  empty patch、2项setup failure；10项进入Agent的实例累计205次coding请求、32次辅助sidecar请求、
  266次工具调用和约366万Provider token。上述终态不能换算成resolved或pass@1。
- 逐条回放真实trace后确认四个Runtime根因。第一，verification planner在缺少exact filename时，会把
  Repo Intelligence返回的通用调用图测试升级为唯一required targeted test；pytest旧仓库因此运行了
  `doc/en/example/assertion/test_setup_flow_example.py`，即使通过也不能证明`rewrite.py`补丁。第二，
  strict离线运行把checkout缺少`astroid`和宿主pytest无法解析旧warning category判为可修复代码错误，
  触发无意义重试。第三，InfCodeX式Sidecar gate位于NZ新增的strict generation gate之前，语义judge
  accept后仍被确定性gate复活，重复付费。第四，`repo-cache`只会读取已有mirror，从未在首次远程clone
  成功时创建mirror，后续同仓库实例继续承担TLS失败风险。
- 对照InfCodeX的Sidecar verifier adapter与observer composition后，NZ没有机械照搬其“Sidecar first”
  顺序：参考实现没有NZ的strict generation verification，因此这里必须按本项目所有权组合。严格状态为
  `unverified/verifying/failed_repairable`时，Sidecar先返回fall-through，由确定性gate负责修复提示；证据
  收敛后才调用语义judge。普通终端模式与`blocked_environment`语义复核保持原行为。
- targeted planner新增最多2500个Python测试文件的有界path-affinity排序，source stem权重大于父目录词；
  `assertion/rewrite.py`现在优先`testing/test_assertrewrite.py`，通用graph候选仍保留为optional证据。该规则
  不依赖Git历史、网络、embedding或新依赖。
- environment classifier只在strict offline边界识别“未出现在本轮changed files中的缺失模块”和明确的
  host-pytest warning API不兼容；补丁自己新增坏import，或本轮修改了`tox.ini/pytest.ini/setup.cfg/
  pyproject.toml`，仍保持`failed_repairable`，没有用宽泛`ImportError`吞掉真实回归。
- 首次仓库准备现用临时目录执行`git clone --mirror`并原子发布到repo-cache；并发worker不会读到半成品，
  cache创建失败或已有cache损坏仍回退原远程clone路径。后续同仓库实例使用本地clone，避免重复下载。
- TDD分别先复现错误required test、环境误判、Sidecar提前调用和cache不落盘。相关验证/planner/Sidecar/
  SWE/hooks组合为`255 passed`；全仓为`2562 passed, 21 skipped in 194.98s`，本轮文件Ruff、compileall与
  `git diff --check`通过。本轮没有重跑付费SWE或官方harness，因此只确认源码控制流闭环，成本和resolved
  改善必须由下一次独立小批量轨迹验证。

## A349 — Sidecar证据Authority与Pytest目录语义收口（2026-08-27）

- 继续回放A347而不是立即付费重跑后，确认`pylint-7114`存在Runtime内部指令冲突：strict system明确
  禁止安装/联网，但Sidecar把checkout缺少依赖判成可修复并要求`pip install -e .`；随后通用失败诊断又要求
  “修复根因”。现在`blocked_environment`作为Runtime-owned事实进入Verifier criteria，明确禁止提出主
  Agent无权执行的操作；相同blocked command的通用失败诊断直接跳过，避免再生成相反指令。
- A347中22次“证据足够，请开始修改”的拒绝仍可能被Provider历史工具调用重复触发。Implementation phase
  现在拥有独立的代际计数：第一次给可恢复收敛提示，同一mutation generation第二次转为terminal blocker；
  成功写入后清零。该计数不再复用strict-progress counter，避免同一个调用被两层policy重复计数而提前终止。
- `pytest-5103`的真实patch为5086字节，但旧Sidecar先把普通diff截到2400字节，渲染时又截到1600字节；
  Verifier因此连续三次声称`_visit_any_all_call`方法体不完整，并要求主Agent重复展示源码。InfCodeX原实现只
  提供mutation数量和最多400字符hint，本来不做源码语义证明；NZ在其上增加语义审查后却没有同步升级证据
  合同。现在单文件证据上限为8KB、总diff证据仍封顶12KB，中等补丁完整可见，超大/多文件任务仍有硬预算。
- `pytest-5221`的另一条根因是项目目录约定：测试位于`testing/python/fixtures.py`，而通用分类器只认识
  `test/tests`，导致source+test scope永远不算localized，Agent探索17轮后才被20-call硬门截停。`testing/`
  现进入跨项目测试目录集合；同时`fixture`不再因包含子串`fix`误判，SWE包装输入会提取Problem statement
  首行作为验收项，并在其他marker均不命中时仍归为coding feature。确定性回放证明同样的source/test证据
  在第6次调查后进入implementation phase。
- 相关组合回归`318 passed`，最终全仓`2568 passed, 21 skipped in 200.57s`；Ruff、目标模块compileall和
  本轮diff whitespace门禁均通过。没有调用Provider、恢复Lite批次或运行Docker harness；这里记录的是
  trace-supported源码修复和离线反事实，不宣称实际token下降、patch正确率或resolved率已经提升。

## A350 — Strict Pytest从“拒绝模型习惯”收口为本地源码执行合同（2026-08-27）

- A347的7次`network-capable or indirect shell syntax`拒绝逐条分类后，5次是合法窄pytest后追加
  `2>&1 | tail -N`，另2次是包安装或分号连接的任意Python探测。直接放开管道会让tail退出码掩盖pytest
  失败；全部拒绝又浪费模型轮次。Runtime现在只规范化“单一、已经通过strict grammar的pytest + 有界
  head/tail显示后缀”，去掉后缀后执行生产者。包安装、任意Python、多命令、其他pipeline保持原样拒绝；
  实际执行命令、原请求和规范化事实进入Tool metadata，失败pytest仍返回真实非零exit。
- `pytest-11148`的20项失败显示测试文件来自checkout，但`_pytest.pathlib`来自主机pytest 9，产生本分支
  API中不存在的required keyword错误。strict policy同时禁止editable install，却没有为src-layout测试
  进程提供本地导入路径。现在只有strict pytest且workspace/workdir内确有`src/`时，Runtime在子进程环境
  前置该绝对本地路径；不修改父进程环境、不联网、不安装依赖，普通终端Bash保持原继承语义。测试用唯一
  本地包先RED复现collection error，再证明checkout source被正确导入。
- 另一处冲突位于Provider wire：通用Bash schema仍写“用于安装packages”，与strict system禁令相反。
  `_active_tool_specs()`现在只在strict context投影直接本地命令描述，明确安装、网络、Git history/remotes、
  `cd`、重定向、任意Python和broad suite禁令；退出strict context后原schema不被全局污染。
- Sidecar 12KB预算也改为真正硬界：`[diff truncated]`标记计算在per-file/total额度内，而不是每个文件再
  超出一个marker。A349的中等单文件完整证据不受影响。
- A350聚焦组合`114 passed`；包含A349/A350的最终全仓为
  `2573 passed, 21 skipped in 189.35s`。Ruff、compileall及本轮文档/源码diff whitespace门禁通过。
  全程未调用Provider、未跑SWE实例或Docker harness；5个历史pytest拒绝和host-package shadowing只是
  trace-supported候选收益，必须由下一次固定小批量验证实际轮次与token变化。

## A351 — Policy诊断单一Authority与Sidecar接受证据复用（2026-08-27）

- A347原始trace共有55次`tool_failure_diagnostic`。逐项与对应`tool_call`对齐后，31次来自已经包含明确
  下一步的`Denied`策略结果，13次来自已经包含合法命令改写建议的strict Bash拒绝，只有11次是真实
  command failure。旧Recovery把前44次再次包装成泛化User消息，造成同一拒绝被Provider历史重复消费。
  当前hook对policy/guardrail/invariant及strict shell的actionable结果只保留原始工具输出，trace记录
  `tool_failure_diagnostic_skipped`；测试失败、环境失败、`old_text`错误和专用Doom-loop换路诊断仍保留。
- A347的Sidecar事件进一步证明“语义judge已接受”没有被当作当前patch generation的事实：7080、11143、
  5227、5413、5495、5692以及7114修复后，同一未变化diff共得到18次accept，其中11次是重复accept，
  没有任何文件写入发生在这些重复审查之间。Sidecar现在缓存由当前User要求、task contract、mutation
  generation、实际diff和风险计数组成的accepted evidence key；仅final wording变化时直接完成。用户要求、
  diff/generation或风险事实变化都会重新调用Verifier，`revise/blocked/provider_error`从不作为accept缓存。
- strict pytest的`src/`识别原本虽然声明有界，却先`list(directory.iterdir())`再切片，面对巨型生成目录仍会
  物化全部entry。当前使用`islice(..., 256)`，整棵探测仍维持512 entry/有限深度上限；纯C/C++ `src/`
  不会误注入PYTHONPATH。
- TDD先得到3个预期失败，再完成实现。首次全仓回归暴露Doom-loop专用诊断被过宽去重规则吞掉，随即把它
  恢复为高价值结构化恢复并通过原端到端用例；最终全仓为`2578 passed, 21 skipped in 238.38s`。Ruff、
  compileall与`git diff --check`全部通过。本轮未调用Provider、未重跑Lite或官方harness；44/55与11/22
  是对保存A347事件的控制流反事实，不冒充新运行的token或resolved结果。

## A352 — “文本中出现路径”不再等于“用户授权修改该路径”（2026-08-27）

- A347 report暴露了另一类跨层authority污染：10项Agent运行的`requested_paths`大多来自issue证据而非
  用户指定目标。典型值包括宿主`/usr/local/.../_pytest/runner.py`、开发者`/Users/.../test_commands.py`、
  traceback里的`collector.c`、Pylint最小复现里的`a.py/a/a.py`以及pytest命令目标。Runtime随后把这些值
  作为“User named target files”写回system reminder，并供closure known-path、implementation bundle、
  hook exact/basename matching消费，真正目标甚至可能因5项上限被挤出。
- `requested_paths`现在只接受与正向写入动词同一有效片段绑定的workspace-relative路径。pytest/tox/nox
  执行片段、`Traceback/stack trace`之后的frame、否定修改片段、Unix绝对路径、Windows drive路径和`..`
  均不能获得mutation authority。验收命令仍由`VerificationContract.targets`保存，普通trace文本仍留在
  Session/Repo检索上下文中，因此这是所有权拆分，不是删除问题证据。
- Bootstrap artifact原先还会把路径组件二次当成semantic surface：即使literal path被过滤，`runner.py`
  中的`runner`或`collector.c`中的`collector`仍可能被全局`fix`动词提升为required behavior artifact。
  新的`explicit_path_allowlist`由Runtime强路径事实传入TaskContract；路径/basename token先从semantic
  surface文本移除，只有allowlist中的literal可成为required artifact。裸`parser/CLI/README`等项目创建
  语义推断仍保持，明确`Fix parser.py`和`update src/parser.py`也保持确定性artifact。
- 续跑场景同步收口：`run pytest tests/test_new_parser.py`只更新verification contract，不再覆盖上一轮
  `parser.py`目标；明确`Continue ... in parser_v2.py`仍会置顶新目标。相关合同/续跑/路径组合`118 passed`，
  最终全仓`2589 passed, 21 skipped in 243.50s`，Ruff、compileall和`git diff --check`通过。本轮仍是
  保存report驱动的离线修复，没有Provider调用或新SWE成绩。

## A353 — Stall Sidecar结构化判定与闭环工具成本去重（2026-08-27）

- A347的10条`stall_sidecar_verdict`全部失败开放：`trace=provider_error`且错误均为JSON空正文解析失败。
  这10条只来自`diff_status`（6次）与`verify_changed_files`（4次），累计34,417.832ms；没有一条提供有效
  nudge。根因是NZ旧实现要求Provider自由返回JSON正文，而InfCodeX FEATURE_178/215明确使用强制
  `report_stall_judgment`工具调用，再交给统一LLM Judge内核做工具名模糊匹配、布尔纠正、超时和失败开放。
- Provider stall路径现使用同一结构化合同：强制report tool、300 token硬上限、5秒双层时间边界，并保留
  旧JSON正文作为兼容回退。DeepSeek V4判定请求关闭thinking，避免短结构化结果落在reasoning-only字段；
  空正文或错误tool call只产生`no_tool_call/provider_error`安全默认值，不再把JSON异常抛回orchestrator。
- `diff_status`和`verify_changed_files`是确定性的本地闭环摘要，不需要付费语义judge。它们现在跳过L2记录，
  但仍进入本地`RecoveryState`连续重复检测：相同调用第三次依然由Doom-loop门禁拦截。因此省掉外部调用
  没有取消无限重复保护；真实read/search等语义重复仍保留InfCodeX的L1→L2路径。
- TDD先稳定复现“结构化tool call + 空content触发JSON异常”和“闭环工具误入L2”两项失败；相关Sidecar、
  Judge、Tool Policy组合`51 passed`，最终全仓`2591 passed, 21 skipped in 219.13s`。Ruff、compileall与
  `git diff --check`通过。按A347保存轨迹反事实，这一规则会消除10/10无效stall调用和34.418秒等待；本轮
  未调用Provider、未重跑SWE或官方harness，不把反事实写成真实token收益。

## A354–A356 — Provider用途账本、Compaction观测与取消闭环（2026-08-27）

- A347的`model_call_*`把22次Completion Verifier和10次Stall Judge全部标成`stall_sidecar`；只能通过
  独立的`sidecar_started`事件反推真实职责。运行时明明已有`ModelCallPurpose.VERIFIER`，Provider adapter
  却传了错误枚举。当前Verifier使用独立`verifier` purpose，后续calls/tokens/latency/error可以直接按职责
  聚合，不再把完成语义审查伪装成卡死检测。
- 更大的账本遗漏位于自动压缩：A347有11条`compact`事件、0条`purpose=compaction` model start，且没有
  payload recovery；源码确认每次都由`auto_compact()`另建一个无observer的Gateway。原报表205 coding +
  32 auxiliary = 237个可见调用，实际还执行了11次summary模型请求，即至少248个逻辑调用；原
  3,662,817 Provider token没有压缩usage，只能作为下界，不能继续称为完整总量。
- `auto_compact`现接收Agent-owned Gateway observer，所有压缩attempts/usage/duration/status进入统一trace、
  `RuntimeState.provider_*`与headless runtime summary，purpose固定为`compaction`。没有把压缩算成coding，
  也没有从`compact`控制事件估算或伪造usage。
- 异步Context Runtime取消现在通过新增窄能力`cancel_compaction`设置当前Gateway cancel event；Gateway在
  调度前或poll边界返回cancelled，`to_thread_settled`等待线程安全结算后再传播`CancelledError`。因此用户
  Ctrl+C不再可能被压缩请求的600秒hard timeout拖住；普通同步压缩和已有Context接口保持兼容。
- TDD先分别复现错误purpose、silent compaction和取消不下传；相关Provider/Context/observability组合
  `115 passed`，最终全仓`2595 passed, 21 skipped in 213.38s`。Ruff、compileall和diff check通过。本轮
  没有发起新Provider调用；248与token下界是对旧保存轨迹和当时代码路径的审计修正，不是新成绩。

## A357–A360 — Vision/Memory统一Provider边界与真正终态账本（2026-08-27）

- 对全仓`ProductionModelGateway`构造点逐一审计后，确认主coding、Completion Verifier、Stall Judge和
  Compaction已经接入Agent observer，但可选Vision描述与LLM Memory提取/重排仍是silent调用。Memory还
  固定退化到OpenAI Chat Completions bridge，即使当前Session实际使用Anthropic/Gemini/Responses原生
  adapter，也会绕过相应wire协议和capability snapshot。
- Vision与Memory现在显式接收当前Resolved Provider、模型能力快照和Gateway observer；purpose分别为
  `vision`与`memory`。因此attempt、真实usage、duration、status进入同一trace和RuntimeState，不再靠
  功能事件猜测成本。Vision和异步Memory同时把task取消下传到Gateway polling边界，避免等待600秒hard
  timeout。
- 终态顺序原先是“先冻结runtime并发出`run_end`，再运行Memory finalize”。即使Memory调用已经可观测，
  返回结果与`run_end`仍会少算最后一笔。Lifecycle现在先构造Assistant终态、再结算同步/awaited Memory，
  最后刷新runtime、持久化并发布唯一`run_end`。若`MEMORY_ASYNC_WRITE`与LLM提取同时开启，Provider工作
  不再脱离run后台执行，避免延迟调用污染下一轮已reset的可变账本；纯本地Memory写入仍可异步。
- Memory取消过去会被“LLM失败则规则回退”的宽泛异常捕获，随后仍保存候选并推进processed-message cursor；
  下次运行永远不会重试该对话增量。现在cancelled outcome使用内部协作取消信号穿透fallback，在候选提交
  和cursor写入前都有取消检查，锁正常释放但不提交任何状态。
- 第二次全仓回归还发现Focused Lifecycle adapter在terminal assertion后把host上的admission violations
  用旧state快照覆盖。adapter现同步该证据到唯一LifecycleRunState，重复runtime summary保持幂等。
- TDD覆盖Vision observer/取消、Memory原生Provider/observer、Service依赖传递、同步/异步终态账本、LLM
  Memory不得detach以及取消不得推进cursor。最终全仓`2603 passed, 21 skipped in 219.68s`；Ruff、
  compileall与`git diff --check`通过。本轮没有调用真实Provider或重跑SWE，结论只证明未来账本完整性与
  生命周期控制流，不补造旧轨迹的Vision/Memory token。

## A361–A364 — 终态语义、模型归因与Sidecar并发所有权（2026-08-27）

- `aborted`现在保留VM/runtime与`last_error`并映射typed `RunStatus.ERROR`；cancelled/interrupted/aborted
  不启动新的终态学习。Memory收尾失败只形成`terminal_learning_failed`，不会抹掉已完成正文或阻止
  `run_end`。
- `doctor --cwd/--workspace`统一目标目录；显式`.product-*` Repo Map可以冷扫描，但不会把私有副本注入
  根图cache，关闭TP-021/TP-022。
- 所有Gateway事件携带provider/model/request-model/variant，Runtime同时按purpose和provider/model汇总
  call、usage与duration，避免辅助模型被误归到主模型。
- Stall L2增加cooperative cancel，终态/初始化/close均先cancel-and-settle。Provider observer和typed
  RunContext加入run-local锁；确定性并发测试证明旧read-modify-write会丢增量，修复后不再发生。

## A365–A368 — Token/Cost账本与Repo后台回调收口（2026-08-27）

- 对照infcode-dev逐step usage结算后，修正Anthropic cache token未进入`total_tokens`的问题。统一归一化
  保证总数不小于input/output/reasoning/cache read/cache write五个互斥桶之和，避免context pressure低估。
- Streaming在稳定边界前失败并转buffered时，内部fallback不再提前发布finish；外层只发布一次合并后的
  logical-call记录，attempts与duration覆盖stream失败和buffered成功两段。
- Gateway已有的provider/registry cost进入Runtime总账，按purpose和provider/model聚合USD，并保留
  unknown-call与cost source计数；Headless JSON在新字段存在时投影，旧fixture/客户端保持兼容。
- Repo预热失败不再无条件启动Watcher；workspace或SQLite在done-callback边界失效时转为可观测的
  `watcher_failed`，不抛Future callback异常或创建幽灵线程。
- 最终全仓`2618 passed, 21 skipped in 213.17s`；Ruff、compileall和diff whitespace门禁通过。本轮没有
  调用真实Provider、恢复Lite批次或运行官方Docker harness，故只证明离线账本与生命周期闭环。

## A369–A370 — Plan审批所有权与窄屏完整信息（2026-08-27）

- `plan_exit`由产品Runtime构造稳定的三分支审批，而不是让模型临时决定问题文案：明确批准后退出Plan、
  当前Session直接实施、或保留Plan继续修改。计划摘要作为完整detail传给交互层，终态选择可持久恢复。
- Fullscreen selector把长问题/计划正文放入独立虚拟化Markdown视口；`PgUp/PgDn/Home/End`滚动详情，
  option区只承担选择。80列下不再以截断的单行description充当审批证据。
- 这两项关闭源码与确定性UI合同差距，但没有调用真实Provider或运行80×24 PTY时延用例，因此真实问题清单
  保持`verify`，不提前写成产品实测closed。

## A371–A376 — Provider逻辑调用账本与RuntimeState持久化（2026-08-27）

- RuntimeState加载时逐字段验证provider ledger；坏的purpose/model bucket、非数值usage、旧版本缺字段
  都在边界降级，不能让一次损坏状态阻断Session恢复。observer对malformed callback也fail-closed。
- Streaming重试及stream→buffered fallback现在聚合为一个logical call：attempts、duration、首token、
  usage和最终状态覆盖全部尝试，只发布一次finish。observer本身抛错不会改变Provider请求结果。
- buffered Provider返回非对象或缺失必要envelope时形成结构化abort，不再在Runner深处以属性错误崩溃。
  RuntimeState写入采用同目录临时文件、flush/fsync、原子replace；部分写入不会破坏上一个可恢复快照。

## A377–A379 — 空完成、输出截断与工具批次容量（2026-08-27）

- `finish_reason=stop`但既无正文又无工具调用不再被当作完成；Runtime先给Provider一次有界恢复机会，重复空
  响应才形成明确错误。`length/max_tokens`保留已生成文本并带continuation context续写，而不是伪装终态。
- 输出限制发生在尚未闭合的tool-call JSON时，截断调用绝不进入dispatch；只有完整且已验证的工具envelope
  才能执行。工具批次容量按“即将调度的完整批次”严格检查，不能在边界多执行一项写操作。

## A380–A382 — Catch历史清理、作用域释放与Provider输入合同（2026-08-27）

- 逐行对照InfCodeX `history-cleanup.ts`/`catch-terminals.ts`后确认：NZ过去只在下次initialize合成缺失结果，
  generic catch仍可先保存Provider-invalid history。新增不可变清理器，只保留与紧邻tool result一一配对的
  非空call ID，保留assistant可见正文；resume、terminal persistence和每次wire projection构成三重防线。
- Native Runner与Production Host对runtime override、broad-test guard和declared test scopes统一使用成对
  context manager；确定性同Task异常测试证明调用者原作用域会恢复。Legacy入口也使用同一execution context，
  Cancelled/KeyboardInterrupt/generic error分别结算typed Session且始终清除host active context。
- Session finalize改为持久化事务：save失败恢复Session status/usage且RunContext仍可重试；再次成功只累加一次
  usage。catch中的trace/save次生失败不覆盖原异常。Provider wire清理会报告移除的孤立call/result数量，
  但不修改durable Session。
- Typed model boundary拒绝bool、NaN、Inf、负duration/cost和非正attempts，防止非有限数进入重试、账本或
  JSON。聚焦Model Gateway为`62 passed`；其余catch/session/projection组合也通过。全仓数字将在本批最终
  fresh run后更新；本段不宣称真实Provider、SWE实例或官方Docker成绩。

## A383–A390 — 损坏状态恢复、可取消重试与严格JSON协议（2026-08-27）

- 自定义`ModelPort`返回的usage、attempt、duration和cost在Runner边界逐字段修复；答案正文不会因坏指标
  丢失，修复事实进入trace。`ModelCapabilities`快照在SDK client创建前验证窗口、输出预算、布尔能力、
  token参数和有限temperature；失败初始化不再先分配再泄漏client。
- Provider投影对异常遗留的孤立tool call执行wire-only清理；若assistant正文为空且调用全被移除，只在请求
  视图补`...`协议占位，不篡改durable Session。终态顺序改为“自定义after hook → Session原子落盘 →
  completed/failed/cancelled事件”，原生`KeyboardInterrupt`也先持久化`INTERRUPTED`。
- RuntimeState、Session transcript、SessionProcessor、Memory proposal/cursor和Context metric ports统一处理
  bool、字符串、NaN与Inf。旧Session按条目隔离坏消息并记录`session_recovery`，不再因一个损坏entry丢失
  整段会话；Context projector异常或非法token值使用本地保守估算并留下`context_metric_repaired`。
- 对照InfCodeX `retry-after.ts`与recovery coordinator后，Provider `Retry-After`只接受正有限值，header最多
  等待120秒；真实sleep使用`Event.wait`或50ms合作轮询，Ctrl+C可中断限流等待。Bash timeout、RunRequest
  `max_turns`、工具曝光pressure和continuation timestamp也补齐`OverflowError`边界。
- 新增共享`json_safety`：结构化Agent输出严格拒绝Python额外接受的`NaN/Infinity`；Session event live树、
  journal与SSE、HTTP请求/响应、headless JSON/JSONL、MCP三种transport、Child Result与Workflow record
  均保证有限、无环、`allow_nan=False`。循环引用形成明确占位，非有限扩展指标降为`null`。
- 以上仍是源码与确定性回归闭环，没有调用真实Provider、恢复SWE批次或运行Docker harness；产品取消时延、
  自定义网关和第三方MCP的真实兼容性仍需下一轮受控实测确认。

## A391–A395 — 恢复所有权、隔离路径与外部进程边界收口（2026-08-27）

- Memory proposal恢复不再信任磁盘中的`confidence/risk/fingerprint/status`组合：候选内容重新规范化并计算
  fingerprint，持久化risk只能提高、不能降低重算风险；非有限置信度降为0并进入高风险人工审核。Change
  manifest、undo state、child state统一使用私有原子JSON写入，崩溃不会覆盖上一份可恢复checkpoint。
- Session Event Bus对replay容量、subscriber queue、recent limit、heartbeat和blocking get timeout逐项验证；
  HTTP service/client、ManagedSession wait、SessionManager close及daemon start/status/stop同样拒绝bool、负数、
  NaN、Infinity和平台不可等待的超长值。由此消除`deque`类型异常、`thread.join(inf)`、socket无限timeout与
  关闭阶段busy loop等跨平台差异。
- Lineage与Agent call stack使用严格JSON并拒绝旧的非标准数值；模型发现/registry缓存、daemon state及
  Provider原生JSON/SSE也改为严格读写。原生Provider响应和SSE累计上限64 MiB，错误正文上限64 KiB；
  Anthropic/Gemini畸形usage逐字段修复，不再因`int("Infinity")`丢弃已成功生成的正文。
- 子Agent恢复新增worktree ownership校验：direct必须等于父workspace，git/copy必须是当前child唯一拥有的
  `.nz-coder/worktrees/<session>`即时子目录。篡改state不能再把resume工具切到workspace外部，也不能在fork
  或reviewed apply时从外部路径复制文件。BackgroundAgentManager的应用入口复用同一ownership authority；
  WorktreeManager同时拒绝目标symlink/非目录和不安全base ref，并给Git参数加`--`边界。
- Workflow project library拒绝`.nz-coder/workflows` symlink逃逸；精确load直接定位project/personal候选，
  不再为加载一个胶囊扫描整个目录；list扫描最多保留4096项内存，胶囊读写严格JSON且原子失败清理临时文件。
  MCP和LSP的startup/request/tool timeout在创建进程前统一验证，非法配置不会遗留半启动server。
  `web_search`与`webfetch`也拒绝bool或非有限/非正timeout，非法工具输入不会被静默夹取后发起网络请求。
- 本阶段首次全仓回归为`2797 passed, 21 skipped in 138.67s`；新增wait、MCP、LSP与apply ownership边界后，
  最终fresh回归为`2831 passed, 21 skipped in 139.49s`。全程没有调用真实Provider、SWE实例或Docker harness，只表示
  源码与确定性恢复合同闭环，不宣称SWE分数或第三方服务实测表现。

## A396 — 三回合预算边界的真实Provider闭环（2026-08-27）

- 在`/home/pyh/test_nzcoder/cron_engine`用真实`openai-compatible/deepseek-v4-flash`执行只读任务：
  “读取README并只报告首个Markdown标题”，显式`--max-turns 3`。修复前运行依次完成`glob_search`，随后
  `read_file`在第2、3回合连续被closure reserve拒绝，最终`status=max_turns`；共3次Provider调用、
  17,511 tokens、0文件修改。trace证明问题发生在Runtime预算门，而不是模型没有找到文件。
- 根因是`WorkBudgetController`在3回合上限仍固定预留2个closure回合，使正常工作窗口只剩1回合。
  TDD新增`test_three_turn_cap_preserves_two_normal_work_calls_before_closure`，RED时`normal_turns=1`；最小修复
  将reserve限制为`min(configured_reserve, nominal_turns // 2)`，因此3回合合同变为“两次正常工作 + 一次
  closure”，4回合及默认15回合的既有两次closure语义保持不变。
- 同任务真实A/B重跑`a396-provider-smoke-fixed-20260827`后，工具链为
  `glob_search → read_file → final`，三回合phase为`normal → normal → closure_repair`，
  `closure_tool_blocked=0`，输出`# cron_engine`，终态`completed/completion_gate_satisfied`且0文件修改。
  `run_end`与终态账本一致：3次coding Provider调用，usage为input 5,125、output 131、reasoning 96、
  cache-read 14,592、total 19,944。
- 定向回归首次暴露一条仍按旧预算算法期待第2回合`convergence`的native-runner断言；同步为
  `investigation`后，相关预算/native/runtime/headless/tool-policy组合为`167 passed`。最终fresh全仓为
  `2832 passed, 21 skipped in 139.36s`，Ruff、compileall和`git diff --check`均通过。全量测试新建的
  `subagent-7e09c202`在确认0编辑、tracked diff哈希一致且57个untracked文件逐一一致后移除，worktree数量
  恢复为74、可用空间恢复到8.7 GB。
- 本项只关闭真实Provider三回合读取链的产品bug；没有执行SWE-bench实例或官方Docker harness，不产生新的
  resolved/pass@1结论。固定SWE观察窗口仍需在磁盘风险处理并完成PTY取消/Plan复测后启动。

## A397 — Plan/取消真实PTY与终态notice持久化（2026-08-27）

- 在真实`openai-compatible/deepseek-v4-flash`与80×24 PTY中复测Plan。Session
  `session-20260827_115550-74ac999b`从`run_start`到审批结算10.97秒，其中`plan_exit`等待用户选择
  628.803ms；按`End`可读到21行摘要末尾`PTY-END-MARKER-8F3C`，按`Home`返回并批准后
  `run_end=completed`。全程4次Provider调用、0编辑，README SHA-256保持
  `9950905ee3c0931d24150018d8cb6e4dc3cd61dd7782f7ba60adfe39830fd0e0`，关闭TP-016/TP-026。
- 运行中Ctrl+C首先证明控制面已正确：0.184秒回到IDLE，Provider finish、run_end和Session assistant均为
  cancelled，0 tools、0 edits、无迟到事件；但80×24 idle屏没有持久终态卡，只短暂显示CANCELLING。
  根因是`TerminalRunRenderer`把`Run cancelled`写入`_run_output`后，`StreamingRenderer.finish()`调用
  `surface.end_run()`立即清空同一临时列表。
- TDD先复现“end_run后只剩Session文本”的RED。Fullscreen新增独立terminal notice通道：终态summary/footer/
  changed-file信息进入idle notice，工具卡和stream仍按原逻辑清空。修复后真实Session
  `session-20260827_120940-4707f0df`在Ctrl+C后0.204秒回到IDLE；PTY resize触发完整重绘仍包含
  `Run cancelled`，进程exit 0且无traceback。trace仅1次303.405ms cancelled Provider调用、0 tools、
  0 edits、唯一`run_end=cancelled`，其后0事件。
- 聚焦回归暴露一条既有测试隐式依赖其他文件先注册`bash`；按项目副作用注册规则在测试内显式import后，
  单独与组合均稳定。UI/renderer/CLI/headless/smoke组合为`136 passed`，fresh全仓为
  `2833 passed, 21 skipped in 139.61s`。全量与聚焦测试生成的4个`find a symbol`夹具worktree均为
  `edits_this_run=0`、`changed_files=[]`、`has_diff=false`，清理后恢复74个历史worktree和约8.5 GB空间。
- 本轮没有运行SWE-bench实例或官方Docker harness；关闭的是恢复评测前的Plan与取消产品门禁，不产生新的
  resolved/pass@1或token效率结论。

## 2026-08-28 — InfCodeX 产品对齐范围审计与回退（不计对齐编号）

- 放弃并删除未实现的 A398 workspace snapshot shared-blob/硬链接压缩规格。它源于当前机器磁盘占用，
  不是 InfCodeX 产品差距；此前没有修改生产代码或执行存量迁移。
- 回退 `.product-*` 测试目录特判和 Repo Map 对所有未知隐藏目录的剪枝。该前缀由手工测试夹具产生，
  不是产品所有权边界；保留真正的 NZ-Coder 内部目录与依赖/缓存排除，以及独立成立的 watcher 竞态修复。
- 回退 Python `.pth` 启动 traceback 过滤。解释器启动损坏即使进程退出码为 0，仍不能作为通过的验证证据。
- 复核已记录的早期误实现：A009 formatter/fixer 扩展、A047 read-episode/语义失败熔断、A105 普通 fork
  task-child 建模均已在原轮次撤回，当前源码搜索没有残留对应字段或分支，因此未做二次回退。
- 新增行为回归证明 `.product-catalog`、`.ci-tools` 等用户目录可被 profile/search/repo-map/snapshot读取，
  而 `.nz-coder`、`.nz-coder-runs`、缓存和依赖目录仍按产品合同排除。此项是范围纠偏，不宣称新增
  InfCodeX parity、Provider/SWE 成绩或磁盘治理能力。

## A398 — 基于物理容量的工具结果准入与最大项优先溢出（2026-08-29）

- **InfCodeX capability：** v0.7.69 `applyToolResultBatchGuardrail`先计算下一次请求可用的真实容量；完整批次
  能放下时逐字保留，只有总量越界才按原始结果从大到小逐项spill，并在每次替换后按实际artifact marker
  重新计数。批次固定外壳为4 tokens，每个`tool_result`再预留4 tokens。
- **NZ gap：** `ProductionToolResultProjector`过去取`min(fallback_cap, physical_capacity)`，导致大窗口即使有
  100,000 tokens可用，18,750-token结果仍会被压到约6,704 tokens；Bash还会先按
  `CONTEXT_TRUNCATE_CHARS`截断，统一投影器永远看不到完整子进程输出。旧的adaptive water-fill会在只spill
  一个最大结果即可解除压力时仍同时截断多个较小结果；`on_post_tool_use`也绕过投影文本读取raw output。
- **NZ implementation：** 有真实capacity callback时由它成为批次正文的唯一预算来源，并扣除`4 + 4N`
  协议外壳；批次完整可放下时不再受fallback cap限制。越界时改为largest-first逐项spill，每次按实际投影
  token重新结算，artifact writer接收完整原文。Bash最终`ToolOutput`保留完整捕获内容，仅实时progress
  metadata保持有界；所有post-result hook统一消费已准入文本。
- **Why not mechanically copied：** 没有复制InfCodeX Bash独立spool路径。NZ已有完整stdout/stderr捕获、
  Session级artifact writer和统一`ToolResultProjector`，因此继续只保留一个spill/provenance owner，避免
  Bash层与投影层产生两份artifact、两种路径和不一致生命周期。
- **Verification：** 严格TDD分别观察到大窗口仍spill、外壳未预留、Bash提前截断、hook读取raw output、
  water-fill误伤较小结果和Subagent阈值预写重复artifact六类RED，再以最小改动转GREEN；混合批次断言只有
  最大结果落artifact，writer收到完整原文。真实`deepseek-v4-flash`三题Session保留了10,198和32,928字符
  tool result，均无旧4,015字符模型侧截断或spill marker；trace中的4,015仅是诊断预览。评测同时暴露当前
  `swebench`要求`--clean true`，wrapper合同经RED→GREEN修复。最终Ruff、compileall、diff check、相关
  `386 passed`与fresh全仓`2841 passed, 21 skipped in 210.62s`全部通过。
- **Remaining gap：** 固定样本实际是历史上已评测过的seen regression smoke，不是此前误判的unseen样本；
  3题各一次真实推理后，官方Docker harness为`1 resolved / 2 unresolved / 0 errors`，仅
  `django__django-11001`通过，不能外推Lite 300或归因成相对旧版本提升。三题都耗尽20次coding call，另有
  1–3次compaction，合计记账1,357,999 tokens；结果容量已对齐但收敛效率仍未闭环。artifact持久化失败时的
  fail-loud/数据保全语义仍与InfCodeX不同，留待独立设计，避免在本轮无边界扩张。

## A399 — InfCodeX式软收敛与验证命令来源边界（2026-08-29）

- **InfCodeX capability：** V2 Worker的工具面不按阶段裁掉调查工具；`planBeforeMutate`只返回warning，
  managed-task budget注入剩余预算提示而不拒绝阶段工具，文本完成再交给Sidecar Verifier作
  `accept/revise/blocked`判定。只读inspection明确包含`git status/diff`。因此“已经知道得够多”不能成为
  runtime替模型决定何时首次修改的硬门；安全、显式任务约束和精确重复检测仍是独立边界。
- **NZ gap与根因：** A398三题中，10924和11019都在第6次调查后触发`implementation_phase_blocked`，
  Provider schema还同步隐藏Bash/read/search；11001直到第13次调查才修改并通过。第12次本地化调查又会进入
  第二个硬block。closure阶段连Git-backed workspace的本地`git diff/status`也被当成非验证命令拒绝。
  另一方面，verification scheduler会把文件名/静态图猜出的唯一required pytest当成系统可自动执行命令，
  Django实际出现`tests/admin_scripts/complex_app/models/__init__.py`和
  `tests/admin_autodiscover/models.py`等错误入口。
- **NZ implementation：** pre-edit phase policy保留兼容no-op外壳，不再产生拒绝；首次写入前调查工具与Bash
  始终保留在Provider schema。第12次只注入`STRICT CONVERGENCE`软提醒，只有全局第20次调查硬上限仍可阻止
  无限读取；安全、不可修改测试约束、精确重复和总回合上限未回退。删除对应dead state、旧计数器和旧硬门
  测试。Git-backed closure现在允许经过现有shell只读分类器确认的`git diff/status`，非Git workspace仍引导
  `diff_status`，命令链中的Git写操作仍被拒绝。
- **Verification provenance：** targeted命令新增显式`automation_provenance`；scheduler只自动执行
  `user_declared`、`model_execution`或`failure_evidence`来源，静态图/文件名候选仍能作为建议显示但不会由系统
  擅自运行。精确失败输出标记`failure_evidence`；模型实际执行过的target标记`model_execution`并跨后续写入
  保留。TDD先分别观察到无来源target被scheduler选中、失败/模型命令没有provenance，再以最小改动转绿。
- **Offline verification：** policy/runtime/context/hooks组合为`129 passed`，verification/planner/scheduler/
  native-runner组合为`161 passed`。fresh全仓为`2835 passed, 21 skipped in 161.58s`；全仓Ruff、compileall、
  `git diff --check`均通过。完整回归中唯一旧失败仍要求第12次本地化后直接blocked，已改为端到端软边界合同；
  第20次全局调查后的重复拒绝终止测试继续通过。
- **Seen regression A/B：** 使用与A398相同的`openai-compatible/deepseek-v4-flash`、strict、SWE nominal 20配置，
  对同一组历史seen样本各运行一次。10924在11次工具调查后于第8个coding turn首次源码修改，11001在第4个
  turn修改；两题官方Docker均resolved。11019获得完整20次coding call但没有源码写入，最终empty patch。
  官方报告因此为`2 resolved / 3`，通过ID为10924和11001；不能外推Lite 300，也不能把单次随机模型A/B视为
  稳定提升。三题均为`implementation_phase_blocked=0`、`strict_progress_blocks=0`。
- **Attribution与remaining gap：** trace中所有错误pytest都没有`_nz_runtime_verification_stage`，是模型自行
  调用；自动scheduler只在10924运行一次static `py_compile`，证明静态图pytest自动执行已关闭。三题共63次
  coding、3次compaction、1,150,399 tokens；相较A398本组三题少207,600 tokens，但样本太小且模型随机，
  只作观测。10924在nominal 20后获准3个bounded-emergency回合并有3次宽泛工具拒绝；11019末尾仍有2次closure
  拒绝且无patch。下一步应解决“模型反复选择错误Django pytest入口”和“有充分定位证据仍不写”的决策质量，
  而不是恢复基于调查次数猜测的首次修改硬门。

## A400 — Capacity-only压缩与仓库原生验证入口（2026-08-29）

- **InfCodeX capability与范围：** InfCodeX默认只在物理上下文容量不足时压缩，长上下文的replay成本优化不是
  默认的有损边界；压缩请求由专用summary角色完成而不是继续coding/tool use。验证命令必须来自仓库事实或
  真实失败证据，不能仅因存在`tests/`就假定pytest，也不能把文件名/Repo Graph推测升级成必须通过的硬门。
- **A399暴露的NZ gap：** 三题默认在约24K replay触发提前compaction；模型执行
  `pytest ... 2>&1 | tail`时，FD duplication被shell分类器误报为workspace mutation，造成没有文件改动的
  phantom mutation。Django仓库明示`tests/runtests.py`，但profile仍仅因`tests/`目录推荐pytest；静态推测
  的related test在strict产品组合中又会被升级为required。结果是错误runner、错误target和无补丁收敛互相
  放大，而不是安全拒绝真正阻止了修复。
- **NZ implementation：** replay-cost压缩默认值改为0，仅由
  `NZ_CONTEXT_REPLAY_COMPACTION_TOKENS`显式opt-in；压缩请求加入`TEXT ONLY / Do NOT call any tools`专用
  system约束。命令策略区分`2>&1`/`1>&2`等FD duplication与`> file`真实写入。Python profile检测到
  `tests/runtests.py`后优先提供原生runner；planner把Django测试路径转换成dotted selector，并始终保持
  filename/graph候选optional，只有真实失败等provenance-backed目标才required。pytest若命中Django
  settings/bootstrap特征且仓库存在原生runner，Recovery直接给出精确native命令并明确不修改production source。
- **Offline verification：** TDD覆盖默认压缩、专用summary角色、FD duplication、真实重定向、Django profile、
  dotted selector、strict advisory target和runner-mismatch recovery。相关组合为`353 passed`；fresh全仓为
  `2842 passed, 21 skipped in 156.18s`，全仓Ruff、compileall与`git diff --check`均通过。旧的production
  composition测试仍要求把静态猜测升级为required，根因审计后将其改为验证“目标可发现但保持optional”，
  单测与planner/manager组合为`112 passed`。本段只证明离线合同，真实Provider与官方Docker结果见后续实测。
- **Seen regression实测：** 与A399保持同三题、`openai-compatible/deepseek-v4-flash`、strict pass@1、每题
  一次、nominal 20和900秒题级超时。10924在第7 turn首次编辑，11次coding加1次Verifier后完成；11001也在
  第7 turn编辑，8次coding后完成。11019执行完整20次coding，直到第20 turn才首次`apply_patch`，因此虽然
  从A399的empty patch变成5,960字符非空patch，仍以`max_turns`结束且没有机会运行targeted native test。
  三题均为0次compaction，证明默认capacity-only路径真实生效；Runtime记录的4次edit与实际文件修改一致，
  没有由FD duplication制造phantom mutation。
- **Official Docker结果：** harness一次运行完成`3/3`、`2 resolved / 1 unresolved / 0 empty / 0 errors`，
  通过ID仍为10924和11001，分数与A399的`2/3`持平。差异只在11019从empty变为可判分但错误的patch；这不是
  pass@1提升。官方FAIL_TO_PASS中5项成功、11项失败，PASS_TO_PASS全部成功。失败补丁保留二参
  `Media.merge(list_1, list_2)`而另设私有`_merge_lists(*lists)`，官方测试直接调用`Media.merge(*lists)`时
  报错；独立列表tie-break得到`[1, 2, 3, 4]`而不是`[1, 3, 2, 4]`，冲突warning文案也不符合合同。
- **Cost与remaining gap：** A399为63次coding、3次compaction、1,150,399 tokens；A400降至39次coding、
  0次compaction并增加1次Verifier，但总量升至1,313,630 tokens（+163,231），其中11019单题930,627。
  capacity-only避免了有损summary，却让未收敛任务持续回放增长的上下文，不能单凭调用数下降宣称更高效。
  11019已经读过正确的`tests/forms_tests/tests/test_media.py`，planner最终仍只把`test_widgets`/`admin_widgets`
  当optional候选；第19次调查后的`grep_search`与`read_file`又分别被closure/global investigation硬边界拒绝，
  将唯一修改挤到最后一回合。下一步应把“模型实际检查过的测试文件”提升为强验证provenance，并继续对照
  InfCodeX把末端调查硬拒绝改为软预算/终态判断；不能恢复早期强制修改门，也不能把静态猜测重新设为required。
