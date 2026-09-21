# Follow-up authoritative task references

基线 `a04217ce90f1ab67e458ea043642c9c3b16ff94c`；开始 HEAD/origin/main 一致、工作树干净。**0 paid model requests**。主验收 pytest 使用现有 seccomp offline launcher 禁止 IPv4/IPv6 socket；最后曾有一次未加 launcher 的本地 fake-only 聚焦检查（183 passed），未调用 Provider；生产环境构建使用受控 model-runtime 替身，不调用真实 Provider、embedding 或 online evaluation。

## 1. Root cause

Initial task authority 已有 retention，缺口在 genuine follow-up round authority lifecycle：恢复后 apply_current_round_instruction 更新 requirements/verification，却没有扩展 retained task specifications。缺失 checkpoint 还需要明确区分“禁止从当前文件重建过去 authority”与“允许捕获当前真实用户刚授权的规范”。

Sidecar 三处真实用户判断仅检查 `_nz_synthetic`，遗漏 legacy control prefixes。统一后补测发现 `_is_grounded_history_report` 在没有真实用户时仍用 `-1` 切片，需要显式返回 False。

## 2. Architecture change

```text
genuine follow-up user message
  → ProductionRunLifecycle.initialize / canonical synthetic rejection
  → existing stable message identity
  → lifecycle.prepare_runtime_state (restore or missing-history fail closed)
  → RuntimeState.apply_current_round_instruction
  → extend_task_references_from_user_instruction
  → shared classify_instruction_paths
  → capture_references through WorkspaceFileAccess
  → new authority epoch + bounded newest-first registry
  → existing persist_runtime_state before Provider/tools
  → canonical read metadata / matching path+hash+size+complete
  → existing Sidecar context / reference_digest / semantic hook
```

初始 bind_task_references 保持 one-shot；没有重置 bound 再 capture，没有第二套 parser、ledger 或 verifier。每次真实 follow-up 可新增 epoch；稳定 message ID 避免当前消息在恢复后再次读取可变规范。普通 Read 不授予 authority，明确 Update 保留 mutation obligation。共享 scope 内支持 `Use the updated SPEC.md as the requirements ...`，suffix 不能穿越后续 mutation verb。

## 3. Changed files

- `runtime/adapters/lifecycle.py`：接收真实消息及身份；恢复/缺失/已完成 session 的当前指令捕获；显式 synthetic 不回退为裸文本 authority；无消息 provenance 的旧式 adapter 调用继续用原 workspace 更新 ledger，但关闭新 authority capture。
- `runtime/execution/run_lifecycle.py`：在已有消息 identity 生成之后传递 genuine follow-up；纯继续任务不把恢复摘要当新授权；既有持久化顺序保持。
- `runtime/execution/runtime_state.py`：集中扩展接口、epoch/最后消息身份持久化与 sanitize；apply-current-round 拒绝 canonical synthetic。
- `runtime/verification/reference_evidence.py`：向后兼容 epoch/message provenance；按 path+epoch 去重；新授权优先合并、显式遗漏；独立渲染与既有 digest 自动包含新字段。
- `runtime/agent/task_policy.py`：在已有 verb scope 内识别 use-path-as-requirements，非文件名规则。
- `protocol/message_schema.py`：加入 legacy requirement-completion-gate / output-limit-continuation / work-budget 前缀。
- `runtime/verification/sidecar_verifier.py`：三处 raw flag real-user 判断统一 canonical helper；无 genuine user 的 grounded-history fail closed。
- `tests/runtime/test_followup_authority.py`：本轮 31 个参数化回归实例。

## 4. Authority epoch semantics

- Initial reference：epoch 0 / initial_user_instruction；旧 snapshot 无新增字段时同样解释为 epoch 0。
- Genuine follow-up：单调递增 epoch / current_round_user_instruction；生产路径带 source_message_id。内部兼容的文本调用可以无 ID；不伪造消息 ID。
- 同路径再授权：新旧两条版本并存，按较新 epoch 在前；同 epoch 保留用户路径出现顺序。只有预算淘汰才移除旧记录，并累计 omitted count。
- Agent 修改文件：不会新增 epoch 或替换任何 retained bytes。
- Synthetic runtime guidance：无新 authority；纯 continuation 也不重新授权过去摘要。
- 不同消息即使正文/hash 相同，epoch/provenance 仍不同，semantic cache digest 不相同。已记录的相同消息 ID 不重新 capture。

固定预算不变：最多 4 条 reference、单条 8192 UTF-8 bytes、总计 16384 bytes。先捕获当前授权，再按旧 epoch 从新到旧填充剩余空间；无法容纳的旧条目计入 cumulative omitted count，新增超 count 也计数。超单文件/当前 round 总正文预算沿用 complete=false/capture_status，不把缺失原文说成完整。预算限制针对 retained registry，历史版本不是无限 archive。

## 5. Security / provenance

WorkspaceFileAccess 原有 policy/anchored bounded read 不变；不读取旧 authority 路径来更新原件。新规范在当前 round 第一个工具之前捕获并写入 snapshot。缺失/旧 checkpoint 不重建 OLD.md，但允许当前用户授权 MIGRATION.md。

同路径多个 epoch 继续用真实 canonical read metadata 的 content hash、size、完整性匹配；读 B 只观察匹配 B 的版本，不能把 A 标成已读；读 C 也不能观察 B。若两个 epoch 内容完全相同，一次完整读取可以观察两个相同版本，这是按内容身份的规则。

Snapshot 严格校验 epoch 非 bool 合法非负整数、来源与 epoch 一致、message ID 类型/格式/长度；继续复用原 path/hash/text/budget 校验。它是 host-owned snapshot 的一致性验证，不新增密码学认证机制。不会接纳模型叙述作为 Runtime authority。

## 6. Synthetic-message consistency

`_transcript_has_tool_use`、`_is_grounded_history_report`、`compose_gate_decision` 的 `not message.get("_nz_synthetic")` 全部改为 `not is_synthetic_user_message(message)`。既有 `_extract_current_turn_user_queries` 已使用 canonical helper，保持。

无 assistant-side synthetic 判定变更。真实 WorkBudgetController 通知使用 Runner 的 `_nz_synthetic=True`，新增回归覆盖；legacy stop-hook/reflection/system-reminder 无 flag 也被排除。未修改 `_default_verifier_verdict`、semantic acceptance trace 规则、CompletionGate、权限、轮次、review_run_evidence、stall 或 compaction。

## 7. Regression tests

| 场景 | 证明 |
|---|---|
| restored follow-up 新规范 | 原文/hash/complete/来源保留；reference 不进 mutation obligation |
| 同路径 A→用户再授权 B | B/A 按 epoch 并存；workspace mutation 本身不创建 authority |
| canonical 实际 read_file B | B observed，A 不受影响；另测 C hash 不匹配 |
| snapshot/旧 snapshot | provenance/原文/observed 保持；旧字段兼容；损坏 epoch/source/ID 拒绝 |
| count + byte budget | 新授权优先，稳定顺序，遗漏正确，容量不增长 |
| message replay + cache | 同消息重入不读取新文件；新消息同正文仍改变 digest |
| synthetic 类型矩阵 | stop-hook、completion gate、output-limit、system reminder、实际 work budget 都不能创建 authority |
| 普通 read / explicit update / mixed scope | context、mutation、task_reference 不混用 |
| legacy Sidecar gate | query extraction、gate intent、tool-use 边界、grounded history 一致；没有 genuine user 则 grounded=False |
| production lifecycle 四类 checkpoint | Native production environment + ProductionRunLifecycle + RuntimeState + ToolExecutor/file tool；持久化先于第一个 mutation；原文独立于 rolling transcript 到达 verifier prompt |
| suffix scope / flagged adapter | use-as-spec 不跨修改指令；synthetic 消息不退化成 genuine text |

生产回放 JSON 位于 `replays/{retained,legacy,missing,completed}/replay.json`，保存工具前持久化状态、消息 ID、工具执行事实、工具后文件/retained 内容与 verifier prompt。未运行真实模型，未把受控 prompt 输入断言当作真实 semantic acceptance 或真实 coding 改善。

## 8. Test commands and real results

完整实际 argv/cwd/exit code/耗时见 [commands.jsonl](commands.jsonl)；每项对应原始输出。各组重叠，不相加。

| 阶段 | 结果 |
|---|---|
| a04217c 新回归 before | 11 failed / 2 passed |
| 首轮 focused | 233 passed |
| lifecycle/budget 扩展 | 27 passed |
| scope + explicit synthetic adapter before | 2 failed / 27 deselected |
| focused-final | 315 passed |
| 加强 grounded-history replay-final | 3 failed / 26 passed，已由下面修复 |
| replay-confirmed（含 Sidecar） | 67 passed |
| 实际 work-budget builder | 1 passed / 29 deselected |
| broad（中间版本） | 1584 passed / 11 skipped |
| final-confirmation | 316 passed |
| 本地路径误写 | exit 4 / no tests ran；纠正后 183 passed |
| release-focused（最终实现） | 317 passed |
| release-broad（最终实现） | 1588 passed / 11 skipped |
| Ruff / source diff check | passed |

可复跑形式：

```bash
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_followup_authority.py tests/test_sidecar_verifier.py
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_followup_authority.py tests/runtime/test_task_reference_evidence.py tests/runtime/test_bootstrap_artifacts.py tests/runtime/test_reference_scope_followups.py tests/runtime/test_requirement_scope_runtime.py tests/runtime/test_run_lifecycle.py tests/test_runtime_state.py tests/test_sidecar_verifier.py tests/test_message_schema.py tests/test_task_policy.py
```

Broad 的精确完整命令保存在命令索引，覆盖完整 tests/runtime、tests/security、architecture，以及 loop、verification、permissions、stall、edit recovery 等相关模块。最终 release-broad 已在最后全部代码改动后重跑：1588 passed / 11 skipped，进程自然 exit 0。pytest 断言阶段 228.16 秒，命令总耗时 285.593 秒；退出期间观察到仍存在的 LSP 子进程，未终止进程或修改实现，最终自行结束。commands.jsonl 中 focused-final-latest 标签误重复：第一次是路径错误（exit 4，无测试），同名 txt 已被第二次 183 passed 输出覆盖；本报告明确保留这项采集限制，不将它记作通过。其余 red/green 原始日志独立保留。

## 9. Remaining limitations

固定预算会丢弃较旧 reference，verifier 能看到 omission；不会扩大 memory。缺失历史原文无法恢复。共享 classifier 仍是保守句法，未扩展任意语言、无扩展名/带空格路径。无 message ID 的内部旧式调用不具备跨重入幂等保证，生产 follow-up 已传稳定 ID；用户主动修改 authority 的新 epoch 只在真实生命周期边界捕获。

本轮没有真实 Money M/Q、InfCodeX 对照或付费重测，不声称成功率/效率/提前结束改善。

## 10. Commit / worktree

实现与测试以 `source-hashes.json` 和 `implementation.patch` 冻结；历史 evidence 不修改。本报告在提交前生成，包含于此次 main 提交；实际 SHA 与远端状态见交付消息，可用 `git log -- docs/evidence/followup-authority-2026-09-21/README.md` 查询所属提交。

原始 pytest 失败日志及嵌入的 unified diff 包含尾随空格，完整 staged diff-check 会提示这些原件；不清洗原始证据。生产源码和测试的 diff-check 通过。
