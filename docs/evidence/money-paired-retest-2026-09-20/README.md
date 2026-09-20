# 金额任务真实配对复测：requirement scope 修复后

**这一次两侧都没有完整交付成功。** NZ 做出了通过全部冻结验收的中间实现，随后根据与规格冲突的辅助审查反馈改回旧输出格式，最终 17 pass / 2 fail 且 max_turns；InfCodeX 在创建待办后报告 success=true，业务文件完全未变，验收失败。就本样本的实际实现能力，NZ 更有进展；就最终交付，两者都不合格，不能把任一运行算作成功。

原 REQUIREMENTS.md 必须修改的错误义务已经不再出现，真实 completion review 在第 15 主请求后正常触发。新的直接阻塞是 semantic review 要求修订；审查缺少原规格正文并错误要求保留旧格式，是下一处有证据支持的问题。本次只取证，没有继续修改生产代码，没有重跑样本。

## 1. 配置、授权与版本

| 项目 | 本次固定配置 |
|---|---|
| NZ commit | `d5655a9996076fc614835367b1cc53cf5d3c3bc7`，包含 required-artifact scope 修复；运行期间工作树干净 |
| InfCodeX commit | `d3a812379b589597347f5be12d5b68477e577f02`，固定 SA CLI，无源码改动 |
| 任务与初态 | 原复杂金额 M；同一 prompt、10 个初始文件、REQUIREMENTS、初始 Git commit、冻结验收；见 [manifest.json](manifest.json) |
| 模型 / Provider | 两侧 `deepseek-v4-flash` / `https://api.deepseek.com`，采集代理原样转发实际请求 |
| 入口 | NZ NativeSDKRunner → AgentRunner 生产链；InfCodeX SA CLI / runSubstrate |
| 请求上限 | 每侧最多 24 主 + 8 自然触发辅助，物理 HTTP 请求计数，重试也占预算；各侧一次，无自动重跑 |
| 输出限制 | NZ main max_tokens=64000；InfCodeX max_completion_tokens=32768；实际 NZ verifier max_tokens=1024 |
| thinking / effort | 主请求均不显式设置；生产 verifier 自然使用 thinking disabled |
| 权限 | 原 NZ 严格 callback + 内置 guard；InfCodeX auto-in-project；两侧授权面有差异 |
| 网络 | Agent/工具子进程 seccomp 禁止 IPv4/IPv6，只通过 Unix socket 访问采集代理的 chat/completions；无 embedding、在线评测、依赖安装 |
| 辅助策略 | 保留现有生产语义审查；本次真实触发 2 次 verifier，没有 stall sidecar |
| 顺序 | InfCodeX → NZ，独立工作区与 HOME；不是完整文件系统沙箱，访问轨迹另审计 |
| 新授权 | 用户“那就继续付费测试，看看我的nzcoder与infcodex的差别。完成任务后push一下。”；本次明确限为金额单题两侧各一次，含正常辅助审查，见 [authorization.json](authorization.json) |

这是新的同期金额配对样本；旧 M/N/Q/S 只是历史对照，不冒充本次重跑。只重跑金额题是为了检验刚修复的明确断点，不用多个新题混淆因果。原采集脚本复制到独立目录，执行脚本只改输出/临时目录、manifest 来源和任务数说明；run-nz、capture、reference 启动适配保持原样。没有预填补丁、修改系统提示、扩大权限或手工更新 ledger。

开始时 HEAD、origin/main、实时远端 main 均为 d53605d；已有工作树变更仅为刚完成的本轮修复和回归，先提交为 d5655a9 再运行。本次运行后的提交只增加实验文档和证据。前一轮回归为 820 passed / 3 skipped，两个基线 compaction fake 失败单列，详见 [修复报告](../requirement-scope-2026-09-20/README.md)。源码/测试 diff --check 通过；原始测试输出、原始生成文件及补丁证据中的尾随空白保留，不为消除 evidence 的 whitespace 提示改写原件。

## 2. 最终结果与逐请求事实

| 侧 | 冻结独立验收 | 最终公开测试 | 实际运行状态 | 主 / 辅 | total_tokens（含辅） | 进程耗时 |
|---|---|---|---|---:|---:|---:|
| NZ | **17 pass / 2 fail** | 54 pass | **max_turns**，semantic_review_requires_revision | 24 / 2 | 482,217 | 159.27s |
| InfCodeX | **1 pass / 1 fail / 17 errors** | 原测试 1 pass | **success=true**，无业务文件修改 | 6 / 0 | 125,478 | 38.46s |

完整逐请求事实表（每一个实际请求，不按阶段合并）：

- [NZ：24 主 + 2 辅](M/nzcoder/trajectory.md)，[完整参数、执行结果、下一请求可见历史、ledger/generation](M/nzcoder/causal-table.json)。
- [InfCodeX：6 主](M/infcodex/trajectory.md)，[完整参数和工具投影](M/infcodex/causal-table.json)。

表中 HTTP #16、#26 是 NZ verifier；从 HTTP #17 起主请求号=HTTP号−1。InfCodeX 没有暴露 NZ 式 ledger，记 N/A；隐藏 todo 没有公开 tool.result，不能伪造其内部执行状态。

| 主请求时点 | NZ 实际事实 | InfCodeX 实际事实 |
|---|---|---|
| 调查 | #1 复合 Bash 拒绝，#2 真正读取 REQUIREMENTS，#3 读取五个 caller/原测试 | #1 读需求/README，#2 列文件，#3–4 读源码/测试，#5 环境检查及旧测试通过 |
| 实现 | #4 写中央 Quote API 和五个 caller，#5 写测试 | 无源码写入 |
| 首次测试 | #6 精确 pytest 1 fail / 50 pass：自写断言把 1.005×100 的 minor amount 误写为 101 | 只有旧基线通过 |
| 修订 | #7 将该断言修正为 10050，并另测 1.005×1=101；#8/10/13 Git Bash 被拒，#9 diff_status 和 #11 重读继续；#12 调整格式函数与导出 | #6 输出 5 个 todo_create，finish_reason=tool_calls；其后 Runtime turn.completed / run.result success=true |
| 首次完整候选 | #14 更新测试与 README，V5 是首个全部 19 项通过的冻结版本；#15 精确 pytest 53 pass | 最终文件与初态完全相同 |
| 第一次审查 | HTTP #16 verifier revise，要求 web/jobs 恢复旧形态，与 REQUIREMENTS 明列的新契约冲突 | 无后续请求 |
| 响应审查 | #16 glob，#17 两个 Bash 被拒；#18 批写回旧 web/jobs 格式及相关测试；Runtime py_compile Bash 也被拒 | — |
| 后续测试 | #19 管道测试真实失败，#20 修正另一处自写折扣断言，#21 README 调整，#22 管道 54 pass，#23 静态验证 | — |
| 最终边界 | #24 模型 stop 终答；Runtime 自行精确 pytest 54 pass；HTTP #26 verifier 再 revise，要求 monthly 也恢复 float；耗尽主预算，max_turns | — |

第 #7 的断言修正符合精确乘法语义；不把所有“改测试”都当掩盖错误。第 #18 的 web/jobs 变更却确实违反冻结规格：checkout 从 currency/total_minor 字典改成 total 浮点数字典，export 从 currency,total_minor CSV 改回 total CSV。第 #20 修正折扣测试不弥补该接口回退。

## 3. A–F 与历史断点对照

[机器可读 A–F](boundaries.json)、[原 terminal 事件](M/nzcoder/terminal-boundary.json)、[真实验证事件](M/nzcoder/verification-events.json)。

| 边界 | 新 NZ 事实 | 证据强度 |
|---|---|---|
| A 首次完整正确状态 | #14 后 V5，G14；结束后离线执行冻结验收 19 pass。更早 V1–V4 为 18 pass / 1 fail，仅文档不足 | 强：有当时版本字节/哈希；不是从最终文件倒推 |
| B 必要声明测试真实通过 | #15 `python -m pytest -q tests`，exit=0，53 pass | 强：实际 subprocess/工具结果 |
| C 当前可信验证 | #15 后、HTTP #16 前 VG=G=14，acceptance mutation generation=13（G14 是文档变更）；contract passed=true | 强：实际 state snapshot/contract |
| D 已知完成条件 | R1/R2/R4 satisfied；R3 兼容性 candidate，等待 semantic review；REQUIREMENTS 未改但不是 mutation obligation | 强：确定性前置条件具备，不宣称全部语义条件已接受 |
| E 首次可进入现有 completion path | #15 后 early_tool_completion_candidate=true；触发真实 verifier，revise 后 continue | 强：review 候选边界；**从未出现被接受的最终 completed 边界** |
| F 模型与 Runtime 结束 | #24 finish_reason=stop；第二次审查 revise；最终 max_turns / semantic_review_requires_revision | 强：Provider 与 Runtime 独立事实 |

旧 M 是最终 19 pass、G9/VG9 的可信验证已经存在，但 R1 把 REQUIREMENTS 当必需修改文件，R1 pending，#24 后 hard_requirements_unresolved，未获得 semantic acceptance。本次 R1 错误义务消失，#15 即到达正常 review 候选；这是修复在真实链中生效的证据。但不能把“进入 review”写成“完成成功”，也不能把这条新轨迹与旧轨迹间的全部差异都归因于补丁。

## 4. 新的第一处直接完成阻塞：审查缺少约束正文

[reference-context-provenance.json](reference-context-provenance.json) 保留主模型实际读取 REQUIREMENTS 的 tool result，以及两次审查输入中关键规格缺失的检测；[semantic-review.json](M/nzcoder/semantic-review.json) 保留完整 verdict 与请求前状态；[源码佐证](semantic-source-corroboration.json) 固定到 d5655a9。

实际链条是：

1. REQUIREMENTS 明确要求 checkout 返回 currency/total_minor，export 返回 currency,total_minor CSV，monthly 返回整数 minor units。
2. 主模型 #2 真正读过该文件。#14 后 V5 完整独立验收 19 pass，#15 当前测试 53 pass。
3. 第一审查请求只携带用户任务、最近 transcript、编辑 diff 等信息，**不含上述原规格正文/契约句子**。源码 build_verifier_context 使用最近 24 条消息，不自动保留被引用且未修改文件的完整约束。
4. verifier 将旧测试中的 total/旧 CSV 当作“documented format”，要求恢复。其 revise 作为 stop-hook-guidance 进入后续主请求；这不是用户新要求。既有指导文本还要求把 prior failed todo 当作 ground truth。
5. #18 写入了与该反馈一致的回退；V5→V6 的离线冻结验收由 **19 pass 变成 17 pass / 2 fail**。最终 V8 仍同样失败。
6. 第二次审查又要求 monthly 改成旧 float，仍与原规格“integer minor units”冲突；没有剩余主请求执行该建议，原状态保留 max_turns。

[intermediate-acceptance.json](intermediate-acceptance.json) 对 V0–V8 均在运行结束后、临时副本中执行原冻结验收，未反馈给模型，原 snapshots 不变。这能证明何时出现/丢失可验收版本，不能证明提供完整规格后某次真实 verifier 必然 accept。

**强证据：** 输出契约冲突、verifier 缺少关键规格、反馈可见、后续对应回退、验收下降和终止原因。**有源码支持的机制解释：** rolling transcript 未保留早期读取的 reference 约束。**尚未证明：** 缺失是模型错误判断的唯一原因，或怎样修改能稳定修复；尚未建立该新问题的失败回归。本轮不据此关闭审查、重写 prompt、扩大上下文或追加调用。

原始用户句子“preserve documented formats”单看有歧义，但同一任务明确引用的 REQUIREMENTS 和预先冻结验收对新格式是一致的。报告保留这项任务表述局限，不在事后重写任务，也不让辅助模型反馈替代文件中的契约。

review_run_evidence / completion review interface 仍是之前记录的另一候选，本次主模型没有调用该工具；不能把本次 failure 归因于它。权限拒绝确实耗费请求，见 permissions.jsonl；6 个模型 Bash dispatch 拒绝与 1 个 Runtime static Bash 拒绝单列，均不当作真实测试失败。最终精确 contract 执行通过，不是 verification 未接入或旧 generation 复用导致结束失败。

## 5. InfCodeX 差异与评价

本次 InfCodeX 重现历史 M/S 的行为：finish_reason=tool_calls，只有 todo_create 批次，随后 success=true、limitReached=false。没有业务文件变化，也没有实现新 API。验收的 17 setup errors 来自 quote API 缺失，不是基础设施故障。

固定版本源码的 hidden todo → 可见 toolResults 投影 → 空结果结束分支已在 [历史源码核对](../complex-paired-2026-09-20/source-corroboration.json) 和 [前一轮固定版本摘录](../requirement-scope-2026-09-20/reference-source.json) 留存。本次仍是同一 commit，未用公开 main 倒推实验事实，未复制这种宽松结束语义。

本样本中 NZ 真正完成了大部分工作、曾有全部验收通过的版本，也没有把最终受阻运行报告成 completed；这比未实现便报告 success 的参考侧更接近有效交付。但 NZ 的辅助审查又把符合规格的结果导向回退，最终同样失败，**不能把 NZ 判为本次完整成功，也不能宣称修复提高了成功率**。InfCodeX 请求少/耗时短主要伴随提前退出，不是等质量效率优势。

## 6. Provider usage 与费用

| 侧 / purpose | 请求 | prompt_tokens | completion_tokens | total_tokens | cache hit | cache miss |
|---|---:|---:|---:|---:|---:|---:|
| NZ main | 24 | 436,585 | 24,247 | 460,832 | 161,152 | 275,433 |
| NZ verifier | 2 | 20,627 | 758 | 21,385 | 1,920 | 18,707 |
| InfCodeX main | 6 | 116,955 | 8,523 | 125,478 | 83,712 | 33,243 |

共 **30 主 + 2 辅 = 32 次实际收费请求**，607,695 total_tokens。所有 HTTP 200，usage 齐全，无自动重跑。逐请求原字段见各侧 usage.json；cache 已包含于 prompt，不重复相加。供应商未暴露账单：**cost unknown**，不从 runtime cost=0 推断免费，不推算美元。辅助没有隐藏在“24 轮”之外。

## 7. 保存、审计与结论边界

每侧目录包含 provider requests/responses、工具调用/结果、运行事件、最终文件、workspace.diff、SHA-256、独立验收、原始 usage；NZ 另有权限、RunEvidence、完整 state/requirement/contract、mutation/verification、semantic 与 terminal 事件。私有 reasoning_content 不保存。执行期源文件哈希在 source-hashes.json，离线脚本本身归入最终 SHA256SUMS。

[integrity-audit.json](integrity-audit.json) 验证两侧初态/初始 Git 一致、最终文件哈希、请求预算/usage/配置、结构化隐私字段，以及 975 个历史 manifest 文件未改；原 paid-comparison 的 63 个文件另逐字节对照。访问检查 [access-audit.json](access-audit.json) 未发现读取另一侧、隐藏验收或安装依赖；它是工具轨迹审计，不是完整 syscall 隔离。独立验收始终在冻结副本上执行。

本次证明 required-artifact scope 修复解除旧的直接阻塞，并暴露了新的审查约束保留问题；**没有证明真实自主完成改善**。下一步如处理新问题，应先用这次真实审查输入建立失败回归，再考虑最小修复，不通过提高轮次、放宽 requirement、跳过语义审查来隐藏失败。本轮到比较、报告和提交推送为止，停止付费调用。
