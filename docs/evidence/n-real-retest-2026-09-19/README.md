# 0885c87：一次真实 NZ N 复测

**结果：独立验收通过，Runtime 正常 completed；5 个实际主请求 + 1 个实际辅助 verifier 请求。** 没有重跑、没有额外模型采样、没有修改 Core。用户新增授权原文：`执行真实N测，我授权付费`，覆盖本次真实 N 及任务已说明的生产自然辅助审查。此前 40 个请求仅为历史证据。

本次因果顺序必须保留：**#4 模型通过管道看到 Node 测试输出 → #5 主模型主动 stop → Runtime 补跑精确声明测试并绑定 generation=1 → 辅助 verifier accept → natural_completion 接受。** 不可写成“模型看见当前 trusted evidence 后才终答”，也不可写成 early completion。

## 1. 配置

| 字段 | 本次实际配置 |
|---|---|
| NZ commit | `0885c874b949792723c515ff098c24d31cd238c9`；运行前 HEAD、origin/main、远端 main 相同，工作树干净 |
| 初态来源 | `docs/evidence/autonomous-comparison-2026-09-15/initial/N` 七文件；逐字节复制至 `/tmp/nz-n-real-0885c87-20260919/workspace`，全部 SHA-256 与历史 N 一致 |
| 测试 / REQUIREMENTS | 原文件未修改；没有预填补丁、依赖安装或额外 hint |
| 任务 | 下方原任务全文，未改写 |
| 模型 / Provider | `deepseek-v4-flash` / `openai-compatible`；沿用原 `.env` 私密连接读取方式，经原类型 Unix HTTP 代理；实际 base URL 单存 connection.json，不含凭据 |
| 模型入口 | 原 `prompt.build(memory_block="", skill_descriptions="")` → `build_product_run_environment` → `NativeSDKRunner` → `AgentRunner`；MAIN_PROFILE；stream=true；retrieval 默认，effective=null |
| 主请求上限 / 实际 | 12 / 5；HTTP 出站前和代理均检查实际主请求数；未触发拒绝、重试或重跑 |
| 主模型输出限制 | 五个实际出站请求均 `max_tokens=64000` |
| thinking / reasoning_effort | 五个主请求两字段均未设置；不解释为 medium |
| 权限 | 原 permission callback AST 完全相同，permission_mode=auto；两次 Bash 均 allowed；未放宽 process/Bash 权限 |
| 网络 | 原 offline_exec.py seccomp；Agent/工具不能建立 IPv4/IPv6 socket；仅代理进程可向原上游转发；无 embedding/在线评测 |
| 辅助模型策略 | 生产自然触发一次 semantic verifier，继承主 Provider/model；stream=false、max_tokens=1024、thinking.type=disabled，reasoning_effort 未设置；这是原生产 verifier 配置，没有人为覆盖 |
| 环境 | Python 3.13.12，原 Node v24.18.1，无 Git 的任务工作区 |
| InfCodeX | 固定 `d3a812379b589597347f5be12d5b68477e577f02`；historical paired reference / 非同时重跑样本 |

```text
Make escaped literals usable with new RegExp(..., 'u'), including hyphens. Preserve ordinary literal matching and rejection of non-string inputs. Run node --test literal.test.cjs and report the change and verification.
```

初态来源及 hash：[initial-source.json](initial-source.json)、[initial-hashes.json](initial-hashes.json)。原入口的观测增量见 [instrumentation.diff](instrumentation.diff)：增加出站计数、日志副本脱敏、真实工具结果和状态/文件快照；没有改模型消息、结果或控制流决定。`capture.py` 在原方法执行后读取状态，不手写 ledger。私密 reasoning 字段只在日志副本删除，live 请求/响应原样传递。计数与脱敏离线检查见 [capture-checks.json](capture-checks.json)。单次启动标记阻止误重跑；不使用旧的双侧 launcher。

## 2. 新 N 逐请求事实表

V 是采集到的实际文件版本；G 是 mutation/acceptance generation；VG 是 Runtime verification_generation。余额是**发送本请求前**，含本次请求。所有记录来自真实 Provider；完整原字段、可见消息和 tool_call IDs 见 [provider-requests.jsonl](provider-requests.jsonl)、[provider-responses.jsonl](provider-responses.jsonl)、[causal-table.json](causal-table.json)。

| 主请求 / trigger / 余额 | finish_reason / 模型工具 | 执行与版本事实 | 下次主请求真正可见内容；需求/契约/审查 | prompt / completion |
|---|---|---|---|---|
| #1 initial_investigation / 12 | tool_calls；list_directory、glob_search | 两工具执行；V0/G0/VG=-1；无 dispatch 或 command failure | #2 可见七文件清单与 literal 测试路径。R1/R2/R3 pending；contract 尚未尝试；无 semantic verdict | 7515 / 97 |
| #2 investigation / 11 | tool_calls；read index.js、literal.test.cjs、readme.md | 三次读取执行；V0/G0/VG=-1 | #3 可见原源码、三项测试、说明文档。三个 requirement 仍 pending；contract 尚未尝试 | 7703 / 100 |
| #3 investigation / 10 | tool_calls；edit_file index.js | 真正完成唯一修改，移除转义字符类中的连字符；V0→V1，G0→G1；VG=-1；无 failure | #4 可见 edit diff 及当前验证需求。R1/R2/R3 pending；测试路径未成为 mutation obligation | 8544 / 618 |
| #4 verification / 9 | tool_calls；read index.js；bash `node --test literal.test.cjs 2>&1 \| tail -30` | 读取和命令均执行，权限允许。真实 Node 输出 3 pass/0 fail，但记录的 exit=0 属于管道；V1/G1；**VG 仍 -1，contract 尚未尝试** | #5 确实可见三项通过输出和修改后源码。Runtime 没有把管道输出升级为 trusted pass；三个 requirement 仍 pending；无 semantic accept | 8689 / 888 |
| #5 verification / 8 | **stop；自然语言 final；无模型工具调用** | 模型报告修改和 3 pass。随后 Runtime completion path 经相同权限回调执行精确 `node --test literal.test.cjs`，真实 exit=0，3 pass/0 fail。V1/G1；契约绑定后 VG=1 | 无第六个主请求。精确执行结果进入当前 contract：R1/R3 satisfied，R2 candidate；自然辅助审查 accept 后 R2 satisfied，Runtime completed | 8959 / 209 |

辅助请求独立记录，不隐藏在“5 轮”中：

| HTTP 请求 / purpose | 触发 / 主预算剩余 | 真实结果 | prompt / completion |
|---|---|---|---|
| #6 / verifier（辅助 #1） | #5 natural completion 的 semantic-contract；主预算仍余 7 | finish_reason=tool_calls；调用 emit_sidecar_verdict，verdict=accept；这是辅助结构化裁决，不是主模型再调普通工具 | 2595 / 160 |

共九条真实工具结果：八条由 #1–4 模型发起，一条由 #5 后 Runtime 声明契约发起。全部 executed=true、dispatch_failed=false、command_failed=false；两次 Bash 通过权限回调。#4 的 command_failed=false 不等同于 Node 子进程退出已被可靠绑定。没有 permission denied、cancelled、timeout、review_run_evidence 参数试探、主请求重试、辅助重试或 max_turns。

原始工具输出及 exit/workdir/executed_command 见 [tool-results.jsonl](tool-results.jsonl)，权限见 [permissions.jsonl](permissions.jsonl)。逐请求快照见 [state-snapshots.jsonl](state-snapshots.jsonl)。trace 中 `request_id` 指当时最近一次已发送 HTTP 请求；`provider_turn_started` 发生在发送之前，审计按其 turn 和随后请求快照关联，不能机械把 trace 上的旧 ID 当本轮编号。

## 3. A–F 边界及证据强度

| 边界 | 本次证据 | 强度与限制 |
|---|---|---|
| A 正确补丁首次形成 | #3；snapshot 20 保存刚落盘 V1，snapshot 21 的 G=1；V1 index.js hash 与冻结最终相同；后续无写入 | 强：有当时文件快照，不由最终文件倒推。正确性限于公开和独立验收覆盖的行为 |
| B 必要声明测试首次可信通过 | #5 stop 后，Runtime 真正执行精确命令；tool-results 第 9 行 exit=0、3 pass/0 fail | 强：#4 有真实测试输出，但管道没有 Node 独立 exit fact，故不把 B 记作 #4 |
| C 进入当前 Runtime verification | runtime-full 第 120 行 verification_result=passed；第 124 行 contract executed，generation=1；snapshot 36 首次完整捕获 VG=G=acceptance G=contract.attempted_generation=1 | 强：classification、执行、run/workspace、command/scope、当前 generation 和 contract 均可交叉追溯。主模型没有再收到此事实；辅助请求看到了 |
| D 当前已知完成条件齐备 | #6 辅助 accept 后，snapshot 41：R1 行为、R2 兼容性、R3 声明验证均 satisfied；无 unresolved；semantic generation=1，文件仍 V1 | 强：Runtime 已知条件满足；不把三个测试提升成所有自然语言兼容性的穷尽证明 |
| E 首次合法结束边界 | runtime-full 第 130 行、snapshot 42：natural_completion、finalize、semantic_review_and_ledger_satisfied | 强：此前没有 terminal acceptance；不是第 #4 测试输出后立即满足 |
| F 模型及 Runtime 实际结束 | 主 #5 finish_reason=stop + final；辅助 #6 tool_calls/accept；最终 Runtime status=completed，early_tool_completion_candidate=false | 强：保留原状态，不把工具审批改名为完成 |

精确文件/事件索引另见 [boundaries.json](boundaries.json)、[terminal-boundary.json](terminal-boundary.json)。`verification_result` 发出时 snapshot 34 仍是 VG=-1；Runtime 随后记录声明契约，snapshot 36 才显示完整绑定。不能把“VM 出现 passed”和“所有绑定已完成”压成同一时点。

RunEvidence 的单条 `verification_evidence.generation` 原字段是 null，本报告没有补写为 1。严格 generation 绑定来自同一 run 的 RuntimeState、verification_contract、requirement ledger 及 terminal event；workspace/workdir、无后续 mutation 和 V1 hash 提供同版本交叉证据。原始 null 保留在 [final-state.json](final-state.json)。

## 4. 与旧 N 对照

| 问题 | 旧 NZ N | 新 NZ N |
|---|---|---|
| 补丁 | #7 | #3，唯一 mutation |
| 模型可见测试通过 | #8 精确 Node 执行，#9 看到 | #4 管道输出，#5 看到 |
| 当前可信 Node evidence | 未绑定；VG=-1 | #5 stop 后 Runtime 精确执行并绑定 generation=1 |
| 首次合法完成边界 | 原运行无 terminal acceptance | 主 #5 后、辅助 #1 accept 时出现 natural_completion acceptance |
| 模型是否实际使用已绑定事实 | 未有此事实；没有自然语言终答 | **不能如此解释**：主模型 stop 在绑定之前；使用了管道测试输出，随后 Runtime 利用了精确验证和 semantic accept |
| 最终状态 | #12 后 max_turns，12 次都是 tool_calls | 主 #5 stop，辅助审查后 completed；5 主 + 1 辅助 |
| 第一个新的 completion 阻塞点 | 不适用本次判断 | 本样本未发生未解决的 completion 阻塞。管道证据不足与待语义审查均由既有 completion path 正常补齐 |

本次不是旧完整动作序列重放：模型没有运行基线测试，也没有读取 REQUIREMENTS/index.d.ts/package.json，没有继续 verify_changed_files 或手填 review_run_evidence。更早补丁、不同工具选择和主动终答是自主采样差异，不能全部归因于 0885c87。可以强确认的是：在本次真实生产路径中，新增的精确 Node contract 接入确实被 Runtime 使用并完成合法结束。

静态 stage 最终 unavailable、required=false；optional npm test 未执行，没有覆盖或抹去精确目标 Node passed。任务没有明确要求 typecheck，本次不证明“显式 typecheck 也可跳过”。测试文件没有成为 mutation artifact；未发生通过后再次修改，generation 失效的反例保留在上一预检的已有回归中，不虚构本样本经历。

参考 InfCodeX 只作为 historical paired reference：原 N 同样 12 主请求，经历基线测试、错误 edit、重读、write 修复、Node 测试、存在问题的扩展断言及修正、git diff 失败、再次 Node 测试，第 12 请求 stop。未同期重跑，不描述为“测试一过立即结束”。

## 5. 独立验收

冻结到 [final-files](final-files) 后，另起网络隔离子进程执行原公开测试和历史原独立断言；两者 exit=0。公开测试 **3 pass / 0 fail**；独立断言覆盖 Unicode literal、元字符、精确匹配/拒绝额外字符及非字符串输入拒绝。验收在主/辅助运行结束后进行，未向模型注入隐藏断言。

代码验收：通过所列断言。运行状态：completed。二者分别记录于 [acceptance.json](acceptance.json) 和 [result.json](result.json)，不是用进程 exit=0 代替验收。

最终只修改 index.js，diff 见 [workspace.diff](workspace.diff)。最终 index.js SHA-256：

```text
4094a0ac1f2bf2b659023ec6fe14133c1b8dfd8e667a6c7937b3d0771ce84520
```

全部最终文件 hash 见 [final-hashes.json](final-hashes.json)。REQUIREMENTS、公开测试及其他五文件保持初态；V1 与冻结结果一致。说明文档涉及 character-class embedding，原独立断言未穷尽该范围；保留此覆盖限制，不宣称所有兼容性均已证明。

## 6. Provider usage

| 请求 | 类型 | prompt | completion | total | cache hit（已包含于 prompt） | cache miss |
|---|---|---:|---:|---:|---:|---:|
| 1 | main | 7515 | 97 | 7612 | 0 | 7515 |
| 2 | main | 7703 | 100 | 7803 | 6912 | 791 |
| 3 | main | 8544 | 618 | 9162 | 6912 | 1632 |
| 4 | main | 8689 | 888 | 9577 | 6912 | 1777 |
| 5 | main | 8959 | 209 | 9168 | 6912 | 2047 |
| 6 | auxiliary verifier | 2595 | 160 | 2755 | 0 | 2595 |
| 主请求合计 | 5 | 41410 | 1912 | 43322 | 27648 | 13762 |
| 辅助合计 | 1 | 2595 | 160 | 2755 | 0 | 2595 |
| 全部合计 | 6 | 44005 | 2072 | **46077** | 27648 | 16357 |

全部 HTTP 200；无供应商重试。原 usage 字段及 reasoning token 数（不是 reasoning 内容）保存在 [usage.json](usage.json)。cache input 不重复计数；reasoning token 不再次加到 completion。供应商未暴露真实账单费用：**cost unknown**，不从 runtime cost=0 推断免费，不推算美元。26.83 秒是本次进程 elapsed，不外推效率。

## 7. 结论强度与停止

- **强证据：** 在这一条与历史 N 配对的真实样本中，0885c87 后 NZ 能够正常完成并结束；独立验收通过，结果与此前 max_turns 不同。真实精确 Node 执行进入当前 contract/generation，语义 verifier 接受，终端边界合法。
- **中等证据：** 修复后的 contract 接入对本次 natural completion 的证据补全有直接贡献。尚不能证明它是两次自主轨迹差异的唯一原因。
- **证据不足：** 模型因看到已绑定证据而提前终答、成功率提升、整体 Agent Core 超过参考、benchmark 或普遍 token 效率提升、模型能力提高。

未发现本次需要修复的新确定性 Core 阻塞。预检中零测试/all-skipped 的独立离线问题仍保留，但本次是 3 项真实通过，不把它扩展成新修复专项。B 反例及权限/generation/语义约束未削弱；review_run_evidence 未修改。本样本正常完成且独立验收通过，按停止条件结束，不继续采样或修改 Completion Core。
