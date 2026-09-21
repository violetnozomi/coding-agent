# Retain authoritative task references for semantic review

基线 `e9e24913e419c1dd12ef2e60b745d4422ec6da2a`，开始时 HEAD/origin/main 一致、工作树干净。本轮 **0 paid model requests**；所有模型边界均为本地受控替身，测试通过现有 seccomp offline launcher 禁止 IPv4/IPv6；不调用 embedding、远程评估或安装依赖。

已实现独立于 transcript 的 Runtime-owned original task reference evidence。明确 task_spec 在任务启动、第一条模型请求/文件修改之前安全捕获，后续不因 rolling history、read cache、文件修改或会话恢复而替换。没有增加 ROLLING_BUFFER_SIZE=24，没有改 Runner 终止语义/轮次/权限或 review_run_evidence schema。

## 1. Root cause 与原始依据

[真实金额复测](../money-paired-retest-2026-09-20/README.md) 中，主模型先读取 REQUIREMENTS，V5 全部 19 项独立验收通过；第 15 主请求测试 53 pass。HTTP #16 的 verifier 未收到原规范中的新 API 输出契约，将旧测试行为误当目标规范，要求回退 web/jobs；随后 V6 验收变成 17 pass / 2 fail。HTTP #26 再次要求 monthly 回退旧格式，最终 max_turns。

代码核查发现三个独立缺口：

- Producer：bootstrap 将明确规格路径与普通 context 合并，丢失 task-spec provenance。
- Evidence transport：VerifierContext 只有当前问句、24 条 recent transcript、diff/final/criteria。且 `_render_transcript` 原本不渲染 tool 消息正文，所以单纯增加历史长度也不能保证规范原文可见。
- Authority：revise 被渲染成近似用户 follow-up，REVISE_RETROSPECTIVE 还要求将 prior failed todo 当 ground truth；accepted cache identity 不包含 reference evidence。

本轮没有拿离线 accept 替身宣称真实模型判决变好，只证明生产输入中原始 authority 不会丢失、反馈不会被赋予新用户 authority。

## 2. Implementation plan 与修改记录

修改前计划：[PLAN.md](PLAN.md)。先写原始生产复现、bootstrap provenance、ground-truth 反例，在未改生产代码的 e9e2491 上 **3 failed**：[before.txt](before.txt)。生产复现已执行真实文件工具和测试 subprocess、经过 NativeSDKRunner/AgentRunner/semantic hook，失败位置正是 verifier 缺少 AUTHORITATIVE TASK REFERENCES，非 import/API 缺失假红灯。

| 变更 | 原因与实现思路 | 验证 |
|---|---|---|
| task_policy 统一 path-role authority | 将已有 mutation scope 扩展成 `_instruction_scopes`；classifier 保留 mutation/task_reference/verification/context、authority 与 mutation operation；旧 mutation 提取降为 projection | 英中指定规范、普通 read、否定、Node 单/双角色、create/delete/rename |
| BootstrapArtifact authority | 明确规格 `role=task_reference, authority=task_spec, required=False`；同文件同时被要求修改时保留 mutation 和 authority 两种 provenance | reference 不能进入 expected_artifacts；explicit Update 仍不能跳过 |
| 新 reference_evidence 模块 | 一次性安全原文捕获、有界字段、sanitize、hash observation、render/digest | 原文/hash、预算、失败原因、持久化、篡改/路径反例 |
| lifecycle 初始捕获 | 实际初始化在 adapters/lifecycle.prepare_runtime_state，而非等 planning/verifier。新任务 capture；恢复不 capture | 第一动作即写规范仍保留 G0；有/无/旧 checkpoint 续跑 |
| RuntimeState + loop metadata | observe_tool 新可选 metadata；真实成功执行后传 canonical metadata，非解析 output | 同版本完整读取为 observed；新版本/partial/failed/无 metadata 为 false |
| verifier 独立 section/cache | typed refs 独立于 rolling transcript；cache 使用 metadata 与内容 SHA256 的稳定 digest | 超过 24 消息依然可见；observed/complete/generation 变化使缓存失效 |
| revise authority | guidance 明确是审查发现；保留原规范附回 Main；legacy stop-hook marker 也标 synthetic | genuine instruction 提取排除 feedback，真实 revise→下一请求含原规范 |

中间边界回归 `edges-before.txt` 为 2 failed / 45 passed：发现缺 `_nz_synthetic` 的旧 stop-hook-guidance 会被当真实用户，以及单 scope 旧 100-path 上限造成 omitted count 不准确；均在统一 authority/提取链修复，没有再加平行 parser。

后补 resume fixture 最初把 HOME 放在 workspace 内，被现有 trust-store policy 正确拒绝（resume-focused.txt 3 failed）。只将测试 HOME 移到 workspace 外，未改安全策略，resume-confirmed.txt 3 passed。

## 3. 核心数据流与安全边界

```text
Genuine user task (existing lifecycle synthetic-message filter)
  → shared classify_instruction_paths
  → task_reference / task_spec
  → fresh lifecycle bootstrap (before Provider / tools)
  → WorkspaceFileAccess.stat + read_bytes_with_identity(maximum=...)
  → RetainedTaskReference in RuntimeState
  → persisted sanitized snapshot
  → canonical read_file metadata/hash observation (optional)
  → VerifierContext.authoritative_references
  → independent AUTHORITATIVE TASK REFERENCES section
  → existing semantic review / accepted cache digest
  → revise stays synthetic finding + same retained references for Main
```

mutation scope 与 reference scope 来自同一个 marker/clause 实现；不是文件名白名单。`Read README.md`、`inspect api.py` 只给 context。`described in`、`according to`、`follow`、`specified in`、`requirements in`、按照/根据/依据/遵循给明确规格 authority。单文件可同时有 mutation + task_reference，或 mutation + verification。

TaskContract / RequirementLedger 没有新增正文或 authority 层，expected_artifacts 仍是必须有真实修改事实的产物。原 d5655a9 的 required callers、create/delete/rename、Node scope 等回归继续通过。

捕获复用 WorkspaceFileAccess 和 WorkspacePathPolicy，采用 model-visible read 策略，带 anchored handles、现有 symlink/private-path/traversal 防护。没有直接 Path.read_text 读取规范。外部 symlink、private metadata/credentials、验证后父目录切换均 fail closed。

`ToolExecutionResult` 未改 API，原 metadata 字段保持；loop 只新增向 observe_tool 传 metadata 的关键字参数。read_file 的 identity 来自同一打开句柄，完整读取、offset=1、path/hash/size 匹配，才设 model_observed=True。不从 XML/display text、模型 modified_files 或自然语言声明创建事实。

## 4. 原规范不可被自我修改替代

初始 `hash=A/text=A/G0` 只捕获一次。之后文件变为 `hash=B/text=B`，无论写、删、重新读、bind 重入或恢复，review 仍收到 A。读 B 不能把 A 标成 observed；已经确实读过 A 的 observed=True 不因后续改写变成没读过。

persisted snapshot 严格检查 list/tuple、数量、路径类型/长度/相对性、authority/source allowlist、UTF-8 字节、hash 格式及 text/hash 相符、严格 bool、非负 generation 等。旧 snapshot 没字段时兼容为空，并锁定禁止延迟重捕获。恢复 activation 丢失 checkpoint 时同样不以当前文件冒充原始版本，trace 记录 original_capture_unavailable。

snapshot sanitize 是结构/一致性校验，不是签名认证：保留现有 host-owned runtime snapshot 信任边界；能同时改写 host snapshot 文本和 digest 的攻击者不在这个新模块的认证能力内。

## 5. 固定预算、截断和缺失事实

| 上限 | 数值 |
|---|---:|
| MAX_REFERENCE_COUNT | 4 |
| MAX_REFERENCE_BYTES | 8192 UTF-8 bytes |
| MAX_REFERENCE_TOTAL_BYTES | 16384 UTF-8 bytes |

只收显式 task_spec，按用户出现顺序去重、deterministic。超过数量记 omitted_count，verifier 有显式 INCOMPLETE 提示；单文件或总预算不够时保留 bounded 元数据，`complete=False`、capture_status=size_limit/total_limit，**不保存伪完整的前缀**。这个实现保守地拒绝超预算正文，不为算完整 hash 读取无限文件；失败捕获的 content_hash 为空，显示 unavailable，不能与成功的 SHA256 混淆。

成功捕获为完整 UTF-8 原字节，SHA256 对应原字节；非 UTF-8、binary/null、缺失/unsafe/竞态失败分别记录 invalid_text/unavailable，complete=false。不会在 semantic review 时补读当前可变文件。

引用正文以 JSON string 表示，转义换行/引号、明确标识为文件数据，元数据另列 Path/Authority/Source/SHA256/Complete/Observed/generation/status。render 不再截断正文假装 complete=true。原文最多 16 KiB；JSON escaping 和元数据会有固定有界开销。reference 内容只约束任务语义，不能授予工具权限或覆盖系统安全要求。

## 6. Semantic authority 与 revise

现有 compatibility-review 原则保留：新测试不能单独证明旧行为兼容，除非用户要求改变语义。新增 precedence 明确 genuine user instructions → Runtime retained explicit task spec → prior behavior evidence；旧 tests/实现/README/delta 不得覆盖用户明确要求的新行为，普通文件名或主模型叙述不能获得 authority，complete=false 时不得臆测缺失规范。

REVISE_RETROSPECTIVE 不再含 ground truth，放在 finding 前面确保原 4000 字反馈上限不会先截掉 authority 说明。StopHook 保持 `_nz_synthetic=True`，并单独附入同一份 bounded retained evidence，使 Main 对照规范解决冲突；review 不成为新的用户指令。legacy `<stop-hook-guidance>` 也由共享 message_schema 识别为 synthetic。

cache key 使用 canonical digest，不放大文本 tuple；digest 包含 path/authority/source/content_hash/complete/model_observed/captured_generation/capture_status/omitted_count。sanitize 先绑定 text/hash，因此同一 hash 不允许对应不同文字。

## 7. 实际测试与结果

已保存的测试 argv、cwd、log 和结果见 [commands.json](commands.json)，并以 [SHA256SUMS.json](SHA256SUMS.json) 和 [integrity-audit.json](integrity-audit.json) 校验本目录与历史原件状态。早期部分日志未单独保存完整 argv，索引明确标注 argv_recorded=false，不从结果倒造命令；大组合完整参数见 broad-command.json。可复跑形式：

```bash
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_task_reference_evidence.py
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime tests/test_sidecar_verifier.py tests/test_message_schema.py tests/test_runtime_state.py tests/test_task_policy.py tests/security/test_workspace_file_access.py tests/security/test_workspace_path_policy.py tests/security/test_user_runtime_state.py tests/security/test_instruction_authority_snapshot.py tests/security/test_run_control_lifecycle.py
```

| 实际阶段 | 结果 | 原始输出 |
|---|---|---|
| 原实现复现（3 项） | 3 failed，预期红灯 | before.txt |
| 第一轮聚焦 | 138 passed | first-focused.txt |
| 扩展边界红灯 | 2 failed / 45 passed，之后修复 | edges-before.txt |
| requirement/runtime/security 聚焦 | 344 passed / 1 skipped | focused.txt |
| reference 专项阶段 | 50 passed | references-focused.txt |
| 增加 race/rolling/bounded 反例 | 53 passed | references-final.txt |
| resume adapter 反例 | 3 passed / 53 deselected（此前 fixture setup 3 failed，已修正） | resume-confirmed.txt |
| 大组合（含 loop） | **1040 passed / 4 skipped / 2 failed** | broad.txt |
| 同两项在独立 e9e2491 原基线 | **2 failed**，相同 compaction 断言 | baseline-e9e2491-compaction.txt |
| 完整 runtime + related/security，包含最终 56 项新增参数化测试 | **895 passed / 1 skipped** | runtime-final.txt |
| Ruff / tracked source diff --check | passed | lint.txt |

两项单列失败是 test_context_overflow_stops_after_three_compaction_attempts 与 test_pre_send_and_reactive_compactions_share_one_three_attempt_owner。在 d53605d 的附加核查同样失败，log 单独保留，不冒充 e9e2491。未修改/跳过这些测试。各组有重叠，不把通过数字相加宣称独立测试总数；没有运行整个仓库所有测试。

生产回放证据：[reference-retention](production-replays/reference-retention/replay.json)、[spec-self-modification](production-replays/spec-self-modification/replay.json)、[money-authority](production-replays/money-authority/replay.json)。包含实际文件工具、NativeSDKRunner/AgentRunner、RuntimeState、verification manager、semantic hook 和最终 ledger。金额受控回放保留实际先失败再修复、20 pass 及独立19pass；S 真实 fixture 的 revise→accept 反例仍成立。Provider 裁决是受控输入，仅证明链路与不变量，不证明真实模型必然接受。

## 8. Changed files

- nz_coder/runtime/agent/task_policy.py：共享路径作用域/role/provenance。
- nz_coder/intelligence/bootstrap_artifacts.py：reference authority 保留。
- nz_coder/runtime/verification/reference_evidence.py：有界捕获、sanitize、观察、渲染、digest。
- nz_coder/runtime/adapters/lifecycle.py：真正初始生命周期捕获、恢复保护。
- nz_coder/runtime/execution/runtime_state.py：证据持久化与 canonical observation；旧提取函数 projection。
- nz_coder/runtime/execution/loop.py：metadata 透传。
- nz_coder/runtime/verification/sidecar_verifier.py：独立 context section、precedence、cache 和 revise 语义。
- nz_coder/runtime/verification/hooks.py：修订时向 Main 提供原证据。
- nz_coder/protocol/message_schema.py：legacy stop-hook synthetic 识别。
- tests/runtime/test_task_reference_evidence.py：56 项参数化回归。
- tests/test_sidecar_verifier.py：原测试更新为新 feedback authority 断言。

ToolExecutor、tools/files、TaskContract/RequirementLedger 的现有接口/安全读/修改义务语义复用，未重建第二套。

## 9. 限制与 follow-up

- 明确路径角色仍是保守句法提取，不是任意自然语言理解；未支持无扩展名/带空格路径、所有长距离指代或任意文件格式。无自动 LLM 摘要或新调用。
- 超预算/非 UTF-8/读失败时只保留明确缺失事实，不提供原文/hash；审查不能声称已经拥有全部规则。未来若要保留大规范片段，需单独设计片段覆盖/原件身份，不静默提高预算。
- model_observed 保守要求单次完整、匹配 hash 的 canonical read。多次 partial read 不合并为完整；现有 read_symbol 无 model_read_observation，保持 false，不解析 stdout 猜测。
- 旧/缺失 checkpoint 没有原件时不回填。动态 genuine user 更新 task authority、跨 workspace 迁移的授权版本管理不在本轮范围。
- reference 输入不再缺失并不保证真实 verifier 一定判断正确；prompt precedence 也不能作为数学保证。尚无本轮真实付费反事实试验，不能宣称请求下降或成功率提升。
- review_run_evidence 接口候选仍未修改；历史 compaction fake 两项失败仍待专项处理。

本目录记录前因后果、实现选择、红/绿回归和验证限制；不将每次键盘编辑伪装成独立因果实验，也不保存 private reasoning_content。旧 evidence 原件保持不变。
