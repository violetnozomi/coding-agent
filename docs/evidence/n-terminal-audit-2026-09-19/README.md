# N 的真实结束边界审计

本轮只处理一个根因：**用户声明的精确 Node 测试没有贯通既有验证命令、修改范围和完成契约**。不改 Runner 状态机，不增加轮次，不取消语义审查、权限或必需验证。

审查基线 `2099a0dc211167acffbdc750b35ff308234dcd0b`；开始时 HEAD `accb6b1`，增量只有已发布的付费证据。InfCodeX 固定 `d3a812379b589597347f5be12d5b68477e577f02`。原付费记录未改写。此次没有主模型、辅助模型、embedding 或在线评测请求。

## 证据与口径

原始 [报告](../paid-comparison-2026-09-16/comparison-report.md)、[NZ N 请求](../paid-comparison-2026-09-16/N/nzcoder/provider-requests.jsonl)、[响应](../paid-comparison-2026-09-16/N/nzcoder/provider-responses.jsonl)、[运行时](../paid-comparison-2026-09-16/N/nzcoder/runtime.jsonl)、[权限](../paid-comparison-2026-09-16/N/nzcoder/permissions.jsonl)、[冻结验收](../paid-comparison-2026-09-16/N/nzcoder/acceptance.json)均已核对；原件另在 `/home/pyh/.codex/artifacts/nz-paid-comparison-2026-09-16`。本目录 [causal-audit.json](causal-audit.json)逐请求保存 finish_reason、工具参数、Provider usage、实际出站字段、NZ turn reason/generation、模型可见工具结果 ID、原件行号和 SHA-256。派生报告不复制 reasoning_content。

两侧 N 均是 12 个实际主请求，没有辅助请求或 Provider 重试；NZ 的逻辑轮次也恰为 12。这不表示两侧预算完全等价：

| 出站事实 | NZ N | InfCodeX N |
|---|---|---|
| 模型 | deepseek-v4-flash | deepseek-v4-flash |
| stream | true | true |
| thinking / reasoning_effort | 未设置 | 未设置 |
| 输出限制 | max_tokens=64000 | max_completion_tokens=32768 |
| prompt / completion / total | 120203 / 5741 / 125944 | 280442 / 5981 / 286423 |
| 正常结束 | 否，max_turns | 是，run.result success=true |
| 最终冻结功能验收 | 通过所列断言 | 通过所列断言 |

缓存输入属于 prompt 总量，不重复相加；NZ runtime 单列 cache-miss input 与 cache_read，不能只拿其中之一与参考 prompt_tokens 比。reasoning token 已包含在 completion 中。成本未暴露：runtime 的 cost=0 配合 unknown_calls 不代表免费。未设置 effort 不能写成明确 medium，也未核实供应商默认等价。原 B/N 共 40 个历史请求，本轮全部用于离线分析；F 不计入。

## NZ N 逐请求因果表

每行工具的结果在下一请求才进入模型历史。`T/F` 指真实执行；命令失败与分发失败分列描述。usage 是 prompt/completion，剩余是主请求上限余量，不是 token 余额。

| 请求 / 触发 / 余量 | 实际动作、执行事实与版本 | 模型可见证据、后续影响 | usage |
|---|---|---|---|
| 1 initial_investigation / 11 | list_directory、glob_search，T；V0 | 获得文件候选，供 #2 | 7532/97 |
| 2 investigation / 10 | read literal.test.cjs、package.json，T；V0 | 测试范围与包信息，供 #3 | 7711/70 |
| 3 investigation / 9 | read index.js、index.d.ts，T；V0 | 当前实现及接口说明，供 #4 | 8549/69 |
| 4 investigation / 8 | 复合 Bash 被 harness 拒绝，F；read REQUIREMENTS.txt，T | 无测试进程；dispatch_failed=true、command_failed=false；#5 改用直接命令 | 8987/1427 |
| 5 investigation / 7 | node --test literal.test.cjs，T、非零；grep README，T；V0 | SyntaxError/Unicode regex 失败送达 #6；**未生成 verification_result** | 9247/1180 |
| 6 investigation / 6 | read readme.md，T；V0 | 进一步核对用途，供 #7 | 10209/700 |
| 7 investigation / 5 | edit_file index.js，T；唯一写入，V1/generation=1 | 去除转义字符类中的连字符；#8 带 patch-risk 提醒（测试路径被当成用户修改范围） | 10564/610 |
| 8 verification / 4 | read index.js + 同一 Node 命令，T；V1，3 pass/0 fail | **#9 实际看见通过；runtime verification_generation 仍 -1** | 10911/519 |
| 9 verification / 3 | diff_status、verify_changed_files，T；V1 | 无 typecheck script/local tsc，静态验证 skipped；#10 带 yellow 收敛提示 | 11174/131 |
| 10 convergence / 2 | review_run_evidence，T，返回 failed | files_changed/commands/verification 不符合 helper 读取键；称无 modified_files/diff | 11346/252 |
| 11 convergence / 1 | review_run_evidence，T，返回 needs_fix | 改成 modified_files，但仍用 commands，未提供 verification_results；#12 orange + hard-cap 提示 | 11712/258 |
| 12 convergence / 0 | review_run_evidence，T，返回 approved；V1 | 本次用了 modified_files/verification_results。无后续主请求可见此结果；工具审批不是 runtime 完成结论 | 12261/428 |

12 次响应都是 `finish_reason=tool_calls`，没有自然语言结束尝试被运行时打回。第 9–12 轮没有新文件修改；不能说直到末轮才形成补丁，也不能把全部额外工作归为反复跑测试。#10–12 的参数探索是模型/工具描述问题，本轮不改该工具。

原 `terminal_boundary_settled`：`boundary=streamed_tool_batch, decision=finalize, status=max_turns, reason=tool_boundary_has_no_terminal_acceptance, contract_executed=false, contract_required=false, contract_passed=true, unresolved_requirements=[], semantic_review_pending_before/after=false, semantic_review_generation=-1, early_tool_completion_candidate=false`。

这里 contract_passed=true 是“没有契约”的空成立；空 ledger 也不证明所有自然语言需求满足。#9–12 scheduler 的 kind=none/action=default 不构成 Node 验证。原始运行没有语义 verifier 接受 N 的证据。

## A–F 关键时点与强度

- **A 正确补丁形成：** #7 是唯一写入。初态加记录的精确替换可重建 V1；#8 读取与最终冻结 SHA-256 `4094a0ac1f2bf2b659023ec6fe14133c1b8dfd8e667a6c7937b3d0771ce84520` 一致。本轮真实工具回放再次重建并独立验收。只证明冻结验收覆盖的行为，不追认未知中间版本。
- **B Agent 内必要测试通过：** #8 的真实 Node 子进程输出 3 pass/0 fail，之后无写入。
- **C 证据传播：** 执行链 #8 已获得输出，主模型 #9 看见；VerificationManager/RuntimeState 没有将其关联为当前版本的验收契约。
- **D 全部完成条件：** 功能/声明测试在 #8 后已有强证据，但原运行没有完整需求账本和语义审查，不能断言所有完成条件当时已具备。index.d.ts 还写到 character-class embedding，冻结验收并未覆盖它；参考补丁使用 \\x2d，NZ 只移除连字符转义。保留这一未验证范围，不能叫“所有兼容性均已证明”。
- **E 首次尝试/可合法结束：** 原模型从未发出无工具终答；原运行时在 #12 检查边界，缺少 terminal acceptance。修复后的受控片段在当前测试通过并经受控语义审查后可走**既有** early completion。不能据此推断真实模型会在原 #8 停止。
- **F 实际结束：** #12 工具提交后 max_turns；不事后改成 completed。

首个可定位的信息差是 #5 Node 失败未分类；影响当前版本通过事实的是 #8。**强证据：命令接入缺口存在、可回归修复。中等强度：它是原 N 无法使用 early completion 的贡献因素。证据不足：它单独决定了原模型后四轮行为。**

## 参考 N 的 SA 实际路径与逐轮事实

固定源码链：`src/kodax_cli.ts` 的实际会话配置 → `packages/coding/src/task-engine.ts:dispatchManagedTask` 的 agentMode=sa 分支 → `runKodaX` (`agent.ts`) → `createDefaultCodingAgent` → `coding-preset.ts` substrate executor → `agent-runtime/run-substrate.ts`。函数名含 managed 不表示这次执行了 runner-driven managed worker；不以其 verifier/stall 解释 SA。

| 请求 | 工具事实 / 下一步 | prompt/completion |
|---|---|---|
| 1 | read 六文件，集中定位 | 17417/322 |
| 2 | Bash Node 基线失败；命令 pipe tail 的 shell Exit 0 **不能**等同测试成功 | 18927/1890 |
| 3 | edit，replacement 的 $& 产生意外替换，随后重读核实 | 21186/414 |
| 4 | read 看到损坏内容 | 22026/322 |
| 5 | write 改为 callback，连字符用 \\x2d | 22640/415 |
| 6 | read 确认当前文件 | 23347/58 |
| 7 | Bash Node 输出 3 pass，#8 可见 | 23616/108 |
| 8 | node -e 扩展断言失败，包含测试表达式自身问题 | 23876/526 |
| 9 | 修正该检查后通过，无新增文件写入 | 24982/666 |
| 10 | git diff 非 Git 工作区失败，&& 后的 Node 测试未运行 | 26027/255 |
| 11 | 单独 Node 测试通过 | 28054/138 |
| 12 | finish_reason=stop，随后 turn.completed / run.result success | 28344/867 |

前 11 次都是 tool_calls，#12 才 stop。真实 emitter 的 tool.result 缺少结构化退出状态时仍是 unknown；上表测试事实来自本次实际工具 content，不把全部工具洗成成功。参考同样用了 12 请求并有失败修复/检查错误；不是“参考从不额外工作”的证据。原始 [请求](../paid-comparison-2026-09-16/N/infcodex/provider-requests.jsonl)、[响应](../paid-comparison-2026-09-16/N/infcodex/provider-responses.jsonl)、[stdout](../paid-comparison-2026-09-16/N/infcodex/stdout.jsonl)、[验收](../paid-comparison-2026-09-16/N/infcodex/acceptance.json)可追溯。

## B 的提前结束约束与权限核查

参考 B 的第 5 次模型响应仍是 tool_calls（todo_create）；原 JSONL 没有相应执行完成事实，随后却有 turn.completed、run.result success=true。业务文件无变化，旧公开测试通过并不满足新 render_product/调用方迁移需求；独立验收失败。不能因为 SA 返回 success 就放宽 NZ 完成要求。

NZ B 有六文件迁移，10 个编码请求加 1 个 verifier 请求，冻结验收通过。权限回调确实拒绝了带 `2>&1` 的 pytest、python -c，以及 `process start python -m pytest -q tests`。回调仅允许名称为 bash 的限定前缀，并禁止 `>`、`;`、`&&` 等。因此这些拒绝首先是 harness 的授权口径，不证明标准 pytest 不能运行。

同时，真实 `verify_project_build` 后来运行同一 `python -m pytest -q tests` 并输出 3 passed，未经过上述 process/Bash 回调。源码 `project_creation/verifier.py:_run_one` 用自己的 allowlist + subprocess.run。这是**工具授权面不一致**的独立观察；不能说“运行时验证合法绕过用户拒绝”。本轮不修改权限，新增 Node 回归证明声明契约仍遵守其实际 Bash 权限拒绝。B 的功能验收、是否履行相同授权条件须分开评价。

## 唯一修改：补齐精确 Node 验证的现有接入

| 参考行为 / 本次证据 | NZ 原行为与差异 | 本轮位置 / 对齐方式 | 验收 |
|---|---|---|---|
| SA 模型能读取 Node 测试结果后继续/终答 | stdout 能看见，但精确 Node 不进入验证分类 | verification_planner.py / 适配既有 targeted 分类 | 真实失败与通过均有 verification_result |
| 声明测试用于检验行为 | pytest-only contract；Node 无当前版本契约 | task_policy.py、verification_contract.py / 扩展已有解析 | 精确相对目标；管道、过滤、watch、越界路径不降级成另一命令 |
| 不要求改测试才算修实现 | Run node --test ... 中的 change 一词使测试路径变成 mutation artifact | runtime_state.py / 复用验证子句过滤 | 显式“修改测试”仍保留；仅运行测试不生成修改义务 |
| SA 可终答，但不提供 NZ 同等需求账本保证 | NZ 已有 generation、requirement ledger、early completion、review | **已有机制不改**；不是移植 SA 结束标准 | 新变更失效旧通过、缺产物/B 反例不完成 |

没有改 review_run_evidence schema、提示预算、路径索引、安全授权或 Runner 结束控制器。受控 Provider 只返回记录工具动作及辅助审查 verdict，不直接写文件/跑测试；真实 NativeSDKRunner、AgentRunner、ToolExecutor、投影、VerificationManager、Node 子进程执行。所有模型边界都落到离线 Provider，并拒绝真实网络连接；主/辅助请求独立记录。

## 回归与可复现证据

[最小付费动作夹具](../../../tests/fixtures/paid_n_verification_fragment.json)来自原 NZ #3/#5/#7/#8（省略其余调查，不当成原轮次完整重演），初态文件保留。 [运行时测试](../../../tests/runtime/test_node_verification_runtime.py)在真实工具执行后验证模型请求含失败诊断、当前 contract generation、真实 failed→passed、辅助审查、正常结束及最终哈希。受控 accept **不证明**模型能判断全部需求。

[offline-replay](offline-replay/)保存此次四个实际主请求、一个受控辅助审查请求、完整 runtime/state、diff 和独立复测。无预设“下一步结束”响应；已有 early completion 根据新事实结束。三项公开测试及原冻结独立断言再次通过。假 usage 不能用作线上效率证据。

原 HEAD 用 git archive 隔离，复制相同回归后运行 `test_declared_node_pass_closes_at_tool_boundary`：expected completed，actual max_turns。当前相同回归通过。完整片段还暴露了 Node 测试路径被错列修改产物，促成本轮同一接入链的范围修正；没有用修改提示词掩盖原 N 任务。

新增反例包括：通过后再改代码→当前 generation 复测失败仍 max_turns；旧测试绿但 API/调用方未改→不完成；只读无需 diff→可完成；缺 README→不完成且允许下一请求继续；权限拒绝→未执行、无通过；缺 Node 依赖→真实失败。语义审查走真实 hook，只在辅助 Provider 边界返回受控 verdict。取消/恢复继续由已有真实 Runner 回归覆盖，不增加生产恢复机制。

最终结果：核心/工作流/Skill 471 passed；安全/事务等 78 passed、3 skipped（均为 Linux 上未运行的 Windows junction 语义）；loop fake 96 passed、2 failed。两项失败分别是 `test_context_overflow_stops_after_three_compaction_attempts` 与 `test_pre_send_and_reactive_compactions_share_one_three_attempt_owner`，原 HEAD 隔离副本同样失败。

测试命令和基线失败全文见 [test-results.txt](test-results.txt)。Linux / Python 3.13.12；Node `/home/pyh/.local/lib/python3.13/site-packages/playwright/driver/node` v24.18.1。未运行 Windows、全仓测试或真实模型复测。两项历史 compaction fake 失败已逐名在原 HEAD 复现，不弱化断言，不重开预算专项。

结论：已修复 Node 声明验证的**信息与完成控制流接入**，有原实现失败、当前通过和反例证据。原付费 N 的完整自主行为改善尚未复测，不能宣称编码成功率或整体对齐比例提升。本轮停止于此。
