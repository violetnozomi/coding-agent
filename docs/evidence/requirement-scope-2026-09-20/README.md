# Required Artifact / Requirement Scope 修复

修复了 reference/context 文件被错误升级为 required mutation artifact、从而阻止已满足任务正常完成的问题。金额任务对应的受控生产链回归不再因 REQUIREMENTS.md 未修改而保持 unresolved。

**0 paid model requests**。本轮只做离线回归和受控 Provider 替身回放；没有新真实模型轨迹，没有重跑 InfCodeX。没有改 max_turns、模型/收费配置、prompt、权限、Runner 状态机、completion 终止语义或 review_run_evidence，没有删除 semantic review。

## 1. 基线与真实断点

开始时 HEAD、origin/main 和实时远端 main 均为 `d53605d8acf03af6c8030e06879b0aa0863c9f92`，工作树干净，无用户修改。`0885c87 → d53605d` 仅新增 740 个 docs/evidence 文件，没有生产代码增量。本轮所有旧实验原件保持不变，哈希核对见 integrity.json。

原始证据：[金额报告](../complex-paired-2026-09-20/README.md)、[金额逐请求轨迹](../complex-paired-2026-09-20/M/nzcoder/trajectory.md)、[原 final-state](../complex-paired-2026-09-20/M/nzcoder/final-state.json)。关键原字段另提取为 [original-money-breakpoint.json](original-money-breakpoint.json)。

| 原真实运行时点（主请求编号） | 原始事实 |
|---|---|
| #14 | 金额实现、五个调用方、测试与文档已落盘，但实现仍有金额缩放错误 |
| #16 | 精确 `python -m pytest -q tests` 真实失败：6 failed / 14 passed |
| #17 | 模型修改金额缩放实现；最终 mutation generation=9 |
| #18 | 管道显示 20 passed，不能把管道 exit 当作精确声明命令通过 |
| #20–21 | 静态验证、重读；自然触发一次 stall sidecar；Runtime targeted pytest 另有导入错误 |
| #22（HTTP #23） | 精确 pytest 真实 20 passed，contract_passed=true，当前 verification generation=9 |
| #24（HTTP #25） | 模型 finish_reason=stop；Runtime `max_turns / hard_requirements_unresolved` |
| 最终独立验收 | 19 passed；与“是否正常结束”分别记录 |

最终 R1 的 expected_artifacts=[REQUIREMENTS.md]、status=pending；R2 文档 satisfied；R3 兼容性 candidate，semantic generation=-1；R4 验证 satisfied。主请求 24、辅助 stall_sidecar 1。不是 Node verification 未接入、旧 verification、模型从未终答或轮次不足的证据。R1 是直接可见的错误硬义务；R3 当时尚未得到审查接受，不把它从原事实中抹掉。

## 2. 错误产生点与消费点

[provenance-before.json](provenance-before.json) 在独立 d53605d 工作树运行原提取链；[provenance-after.json](provenance-after.json) 对同一任务/初态运行修改后的提取链。

1. `RuntimeState.set_acceptance_criteria_from_text` 调用 `extract_explicit_mutation_paths`。原实现按分句检查是否出现任意正向 mutation 动词，再把该分句中的所有路径加入 requested_paths。
2. 原任务首句是 `Implement the invoice pricing migration described in REQUIREMENTS.md.`。因为存在 Implement，`described in` 所引出的参考文件也被赋予修改义务。实际不是用户写了“修改 REQUIREMENTS”，也不是仅靠文件名的全局扫描；是**分句中动词与路径的作用域绑定过宽**。`Read X and implement Y` 也能重现同源问题，但不冒充原任务措辞。
3. `AgentRunner._maybe_generate_plan` 将 requested_paths 作为 explicit_path_allowlist 传给 `derive_task_contract`，后者调用 `resolve_bootstrap_artifacts`。
4. 原 bootstrap 记录 `{path: REQUIREMENTS.md, role: behavior, required: true, reason: explicit path}`。原 `_role_for_path` 只把特定文档形态归 docs，REQUIREMENTS.md 落入 behavior；更本质的问题是参考角色在提取时已丢失，后续只有“允许的路径”，无法知道它是约束来源。
5. `derive_task_contract` 将其放进 R1.expected_artifacts。此处 expected_artifacts 被后续 ledger 消费为必须有 mutation evidence 的路径，reference/existence 与 required modified artifact 实际混用。
6. `RequirementLedger.observe_verification` 对有 expected_artifacts 的 requirement 要求先有真实 artifact evidence；REQUIREMENTS 没被修改，R1 没有证据，保持 pending。这个消费原则本身保留。
7. `CompletionGate.evaluate → 现有 production terminal boundary` 读取 unresolved；最终拒绝结束，reason=hard_requirements_unresolved。本轮没有在这一最终层加文件名特判。

交叉核查还包括 RunEvidence.expected_files/modified_files、verification contract targets、当前轮 contract merge 和 completion 消费路径（索引见 artifact-consumers.txt）。RunEvidence.expected_files 来自 blueprint，modified_files 由成功工具结果累积；原金额不是 blueprint 创建任务，其错误义务来自 requested_paths → TaskContract.expected_artifacts，而非模型 review_run_evidence 字段。

修改后同一文件仍在 bootstrap 中保留，变为 `{role: context, required: false, reason: reference or verification path}`；完整任务目标、文件和读取结果都存在。它没有被忽略或删除，只是不再要求自身发生修改。验证文件的 command/scope 仍由现有 verification contract 独立表达。

## 3. 最小实现范围

生产变动只涉及已有 task policy、RuntimeState 提取/事实传播、bootstrap artifacts 和 TaskContract/RequirementLedger；完整补丁见 [implementation.diff](implementation.diff)，最终源文件 SHA-256 见 source-hashes.json。

- 将原分句 mutation 规则收敛为共享 `mutation_instruction_scopes`：read/follow/according to/described in 等参考短语、run/validate 等验证动作结束前一个修改作用域；之后明确的新修改动词可重新打开作用域。中英文否定被保留；文件名在识别动词时被遮蔽，避免 read.py 被当作 read 动词。逗号列表保留同一个动词的多个目标。
- bootstrap 的显式路径与语义 surface 推断使用同一作用域来源。参考路径保留为候选，不再通过 stem 推断重新升级为 required。明确文档目标也不会顺带要求修改另一个 README。
- 路径提取保留完整扩展名和句末标点边界，覆盖 .json/.cjs/.txt/.rst。required target 容量按已有 requirement 的 20 路径边界保留，与 5 条提示用 acceptance criteria 分离，避免“API + 五个调用方”被截掉最后一个。
- 没有声明测试命令时，明确 create/delete/rename 或文档文件修改复用已有 deterministic artifact contract，可由文件事实满足；不创建无测试可完成的 behavior/verification 空义务。一般行为修复且无声明测试的 bootstrap 口径保持原样。
- 同一 requirement 中每个具名 artifact 都必须有事实；原来的 any-member 检查改为 all-members。泛化行为和 verification 仍独立，测试通过不清空未完成的产物义务。
- 通过现有 Requirement 的可选 artifact_operations 保留 change/create/delete；rename 分解成旧路径 delete 与新路径 create。字段经过验证、保存/恢复和合并保留；旧 contract 无此字段时使用原默认语义。没有第二套 parser、ledger、manager 或 verifier。
- 实际成功的文件工具将 creation/deletion 事实传给 ledger。仅编辑一个待删除文件不再满足删除义务；单独复制目标不满足 rename；删除后重建会替换过期的存在性事实。模型 final 中的 modified_files 或 todo 状态不能产生这些事实。
- Node 的显式修改路径即使被归为 test，也必须进入 task contract；它与 `node --test` 的验证义务并存。单纯执行测试不产生修改义务。

这些仍是保守的显式指令提取规则，不是任意自然语言的完整语义证明器。没有批量迁移旧运行中已保存的错误 ledger，也没有改写历史 snapshots。自然语言行为完整性继续由既有 semantic review 审查。

## 4. 修复前失败与修复后生产回放

测试入口：`tests/runtime/test_requirement_scope_runtime.py`。金额片段来自真实初态和 #14 后、#17 前的文件快照，以及真实 #17 修复工具参数，记录在 `tests/fixtures/paid_m_requirement_fragment.json`。没有修改原 fixture/验收脚本，也没有把 REQUIREMENTS 移出工作区。

回放保留 NativeSDKRunner → AgentRunner → ToolExecutor → 真实临时工作区文件工具 → RuntimeState / RequirementLedger → VerificationManager → ProductionCompletionVerifier / semantic hook → terminal boundary。只替换 Provider 边界；主动作和 semantic verdict 是受控输入，不是模型能力证据。测试进程及其子进程通过已有 seccomp launcher 禁止 IPv4/IPv6，Provider resolver 全部替换为本地对象。

| 回放时点 | 修复前 d53605d | 修复后 |
|---|---|---|
| 读取 | 真实读取 REQUIREMENTS 和初态源码 | 相同读取，后续模型请求可见内容 |
| 写入 | 聚合写入实际 pre-repair 业务代码、五个 caller、测试和 README | 同一组文件内容 |
| 首次精确测试 | 真实 subprocess：6 failed / 14 passed | 相同失败 |
| 修复 | 实际 #17 的 apply_patch，进入 generation=2 | 同一补丁、同一 generation |
| 当前精确测试 | 真实 subprocess：20 passed；VG=G=AG=2 | 20 passed；VG=G=AG=2 |
| 产物 | REQUIREMENTS 未变；最终各文件 hash 与历史已验收实现相同 | 相同最终 hash；参考文件未变 |
| 独立 acceptance | **真实执行 19 passed** | **真实执行 19 passed** |
| requirement | R1 pending、R3 candidate | R1/R2/R3/R4 均 satisfied，兼容性有 semantic acceptance |
| 结束 | 6 个受控主请求后 max_turns，无 semantic 请求 | 第 5 个受控主请求后，现有 early completion 候选经 1 次受控 semantic accept，completed |

修复前首次红灯：[before-tests.txt](before-tests.txt)；完整基线确认：[baseline-confirmation.txt](baseline-confirmation.txt)、[baseline replay](baseline-confirmation/money-reference/replay.json)、[baseline 独立验收](baseline-confirmation/money-reference/independent-acceptance.json)。修复后：[money replay](after/money-reference/replay.json)、[独立验收](after/money-reference/independent-acceptance.json)、[workspace diff](after/money-reference/workspace.diff)、[final hashes](after/money-reference/workspace-hashes.json)。

这不是重放全部 24 个历史请求：写入被合并，历史 G9 在回放中为 G2，模型决策由替身控制。回放证明该 scope 断点修复后既有 completion path 可走通，**不证明真实金额模型会用 5 个请求、会减少请求或一定在 24 请求内结束**。

无声明测试命令的 create/delete/rename 反例在 d53605d 同样失败（no-command-baseline.txt）：旧实现不建 contract，会直接 completed。最初将所有无命令行为都建立 contract 又破坏了旧调查后返回回归（no-command-loop-regression.txt），并让真实文件操作完成后仍挂着无验证可满足的 behavior（no-command-positive-before.txt）。最终只对明确文件生命周期/文档操作复用已有 deterministic artifact contract；上述正反例及原 loop 测试均转绿。中间一次长组合被中止，其不完整输出单存 intermediate-broad-interrupted.txt，不能视为最终通过，也不归入历史 compaction 问题。

删除操作反例另先出现红灯：编辑 obsolete.md 被误当成完成 Delete，见 operation-before-tests.txt。它限定了本轮操作 provenance 的补充范围。初期矩阵暴露的句末扩展名、否定、Node 双角色等测试/实现调整均保留日志，不归类为历史失败。

## 5. 正反例矩阵

| 场景 | 结果 / 证据 |
|---|---|
| Read REQUIREMENTS.md and implement X | reference，不要求修改；真实金额回放还验证实际读取 |
| Update REQUIREMENTS.md and implement X | mutation；没改即 unresolved；explicit-documentation-update 证明真实修改后可通过 |
| Follow SPEC.md and change api.py | 仅 api.py 是修改目标 |
| Modify SPEC.md to match api.py | 仅 SPEC.md 是修改目标 |
| Run node --test literal.test.cjs | 测试路径只进入验证 scope；原 0885c87 Node 回归保留 |
| Modify literal.test.cjs and run node --test literal.test.cjs | 两义务并存；测试绿但未修改不能完成，真实修改后可以 |
| 全部金额业务修改 + 当前测试通过 | REQUIREMENTS 未变也可完成；独立 19 pass |
| 金额漏 reporting.py | 原测试中的中央计算聚焦用例可以绿，但五个具名调用方要求中 reporting 未满足，max_turns |
| Create / Delete / Rename | 有/无声明测试命令均覆盖；没有对应事实仍阻止；真实创建/删除/两端 rename 可通过；仅编辑或仅复制不够 |
| 只读 Explain / Inspect | 保留无 diff 可完成的既有 production regression；只读引用不产生写义务 |
| 修改后旧验证 | 既有真实 Node “pass→再改→复测失败”回归通过，旧 pass 不能复用 |
| 模型自报 modified_files + todo completed | 实际 generation=0，无业务 diff，不能完成；todo-self-report 回放 |
| 非法权限 / STALE_READ / 回滚 | 既有权限、edit recovery、workspace mutation、事务反例通过 |
| 存储审查 | 使用真实 S 初态与最终源码：83 pass 后受控 verifier revise，补报结果/限制后 accept；4 主 + 2 辅替身请求，文件 generation 一直为 1 |

漏 caller 反例是明确标注的附加语义场景：在原请求前加入五个具体调用方名称，并选择**现有**中央 API 测试以证明“测试没覆盖遗漏，也不能关闭具名修改义务”。原金额主回放仍使用原文和完整 `python -m pytest -q tests`；未改变原实验或独立 acceptance。

存储证据：[storage-revise/replay.json](after/storage-revise/replay.json)。verifier 的两次裁决从生产 hook 进入，revise 之后没有文件变动，最终 contract/verification/semantic 均绑定 G1。受控语义裁决不声称复现真实模型的审查质量，但证明审查可继续阻止结束并在补报后接受。

## 6. 回归结果

所有命令与完整输出保存在本目录；关键最终命令清单见 commands.json。

- requirement/task policy、completion、verification contract/Node、runtime state、semantic review、B/N、artifact evidence、权限、edit recovery/STALE_READ、stall/recovery、loop 聚焦组合：**820 passed, 3 skipped, 2 deselected**，见 [final-regressions.txt](final-regressions.txt)。
- Node 最终专项：**11 passed**，含新增双重角色生产正反例，见 [node-confirmed.txt](node-confirmed.txt)。与前一组合有重叠，不将两组数字相加宣称独立测试总数。
- 最终受控回放/语义矩阵、原 strict-progress loop 反例及证据写出：**52 passed**，见 [final-replay-capture.txt](final-replay-capture.txt)。
- Ruff 与 git diff --check：通过，见 lint.txt。
- 3 个跳过保留原测试条件，未为本轮新增 skip/xfail。
- `go_on` 恢复预算用例耗时较长但最终通过；独立原基线同一用例为 1 passed / 236.47s（resume-baseline.txt），没有将等待时间或未完成输出算作通过。

两个 compaction fake 失败单列：

1. `test_context_overflow_stops_after_three_compaction_attempts`
2. `test_pre_send_and_reactive_compactions_share_one_three_attempt_owner`

首次全组合结果为 767 passed / 3 skipped / 2 failed（broad-focused.txt）。两者在独立未改生产代码的 d53605d 工作树再次失败，见 baseline-confirmation.txt；相同断言是预期 `[False, True, False]` 与实际 `[False, False, False]` 等 compaction 调用行为差异。最终组合仅排除此两项，未改测试、未修改 compaction 实现，也没有把任何其他失败自动归为历史问题。

## 7. 固定参考与下一阶段

仅核对实验固定版本 InfCodeX `d3a812379b589597347f5be12d5b68477e577f02` 的本地源码，未查询/使用公开 main 推断该版本。源码原文/哈希见 reference-source.json：todo_create 等被标为不可见；tool-dispatch 只投影可见工具结果；SA substrate 对空结果分支以 hadToolCalls=false 结算并可报告 success=true。该分支没有 NZ 这里等价的严格 requirement ledger 判定。结合原 M/S 没有业务修改且验收失败的真实结果，只记录运行循环成功与任务完成的架构差异，不移植其结束语义，不修参考侧。

下一项候选固定为 **review_run_evidence / completion review interface**，本轮未实施。已有独立真实样本至少两个：历史 N 多次试探 evidence 字段；复杂 Q 三次主请求调整 review 参数后才 approved。后续目标是 Runtime 已知事实由 review 直接消费，模型只补充 Runtime 无法推导的语义信息；这不能被写成本轮已解决的问题。

本轮结论强度：真实失败来源、修复前红灯、当前 generation 验证、修复后合法边界及反例均有直接离线证据。真实自主轨迹改善仍需另行授权的真实复测。不能据此宣称成功率提升、真实请求数下降、全面超过参考或 completion 问题全部解决。
