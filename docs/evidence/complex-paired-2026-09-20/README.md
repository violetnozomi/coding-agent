# 三个复杂请求的真实配对比较

在这三题、这套已确认配置下，我更倾向 NZ-Coder 的交付效果：三题最终代码均通过预先冻结的独立验收，Q/S 正常结束，M 代码通过却被 completion gate 挡住。InfCodeX 仅 Q 实现并通过验收；M/S 都在创建待办后由运行时报告成功，任务文件没有变化。并发题两侧功能验收相同，InfCodeX 请求更少、耗时更短，NZ 总 token 更少。

这是三个包含复杂行为要求的小型合成仓库，各侧各一次真实运行。结论只覆盖这批观察，不是总体成功率、Benchmark、模型能力或整体产品领先的证明。没有为改善结果修改 Core、任务、测试或提示，没有重跑失败样本。

## 1. 实际配置与授权

| 项目 | 冻结值 / 实际值 |
|---|---|
| NZ commit | `0885c874b949792723c515ff098c24d31cd238c9`；执行后 HEAD、本地 origin/main、远端 main 一致 |
| InfCodeX commit | `d3a812379b589597347f5be12d5b68477e577f02` |
| 模型 / Provider | 两侧 `deepseek-v4-flash`，`https://api.deepseek.com`，实际生产入口经仅采集的代理转发 |
| 入口 | NZ NativeSDKRunner 生产链（有效内部 runner 为 AgentRunner）；InfCodeX 实际 SA CLI / runSubstrate |
| 初态 | [冻结 manifest](../complex-paired-preflight-2026-09-20/manifest.json)；M 10 文件、Q 8 文件、S 6 文件；同题初始文件 SHA-256、Git commit 一致 |
| 用户任务 / REQUIREMENTS | 各题 prompt 逐字见 manifest；公开 REQUIREMENTS 随工作区提供；独立验收置于工作区外 |
| 主请求预算 | 每次 24；本批授权共 144；物理 HTTP 请求计数，重试也计预算 |
| 辅助预算 / 策略 | 每次最多 8，本批最多 48；按生产逻辑自然触发，未人为添加或禁用 |
| 输出限制 | NZ `max_tokens=64000`；InfCodeX `max_completion_tokens=32768`；每条出站请求均核对 |
| thinking / effort | 所有主请求均未显式设置；NZ 生产 stall sidecar 为 thinking disabled / max_tokens 300，verifier 为 disabled / 1024 |
| 权限 | NZ 沿用原 N 严格 callback 和内置 guard；InfCodeX `auto-in-project`。二者授权面不同，未放宽 NZ 权限 |
| 网络 | 子进程继承 seccomp 网络限制；模型仅经 Unix socket/采集代理访问 chat/completions；本地验收，无 embedding、在线评测、依赖安装 |
| 隔离 | 独立工作区和 HOME。bwrap 受宿主 UID/network namespace 限制不可用，**不是完全文件系统隔离** |
| 实际顺序 | M：InfCodeX→NZ；Q：NZ→InfCodeX；S：InfCodeX→NZ；依次运行，各一次 |
| 授权 | 用户“确认”覆盖本批 6 次运行、每次 24 主 + 8 辅；[authorization.json](authorization.json) |

本批是新的同期配对样本，不将旧 N 当作同期参考，也不改变此前严格 N 复测的 12 主请求口径。此前 N 的结果另存于 [n-real-retest-2026-09-19](../n-real-retest-2026-09-19/)，不并入本批统计。

两侧权限、系统提示/工具体系、生产输出限制不同，因此比较的是这些配置下的产品执行效果，不能把全部差异单独归因于 Completion Core。顺序、缓存和网络时延也未做随机化控制。

## 2. 冻结验收和最终运行状态

| 任务 | 侧 | 独立验收 | 最终公开测试 | 原始运行状态 | 主 / 辅请求 | Provider total_tokens（含辅） | 进程耗时 |
|---|---|---|---|---|---:|---:|---:|
| M 金额 API 与五处迁移 | InfCodeX | 未通过：1 pass / 1 fail / 17 setup errors | 1 pass（原测试） | `success=true`，任务文件未改 | 3 / 0 | 73,219 | 40.11s |
| M | NZ | **19 pass** | 20 pass | `max_turns` | 24 / 1 | 451,078 | 144.18s |
| Q 并发、取消、失败收尾 | InfCodeX | **7 pass** | 37 pass | `success=true`，模型 stop | 9 / 0 | 404,577 | 113.20s |
| Q | NZ | **7 pass** | 27 pass | `completed` | 16 / 1 | 289,659 | 192.91s |
| S 版本化存储与原子保存 | InfCodeX | 未通过：1 fail / 18 setup errors | 3 pass（原测试） | `success=true`，任务文件未改 | 3 / 0 | 64,277 | 25.32s |
| S | NZ | **19 pass** | 83 pass | `completed` | 12 / 2 | 259,870 | 173.98s |

M/S 的 setup errors 来自所需新 API 根本不存在，并非无关的评测基础设施错误。两侧旧公开测试原本就通过，不能把 baseline green 当作新需求完成。公开测试由模型增补/修改，因此比较功能质量以预先冻结、双方一致的独立验收为主；37 个公开测试不自动优于 27 个。

完整独立验收 stdout、命令、退出码分别在每个运行目录的 `acceptance.json`；最终文件在 `final-files/`，diff 为 `workspace.diff`，逐文件 SHA-256 为 `final-hashes.json`。验收在最终文件的独立副本上运行，不向主模型回馈结果。最终验收通过只证明这些公开需求检查通过，不证明所有未枚举输入都正确，也不倒推中途某版代码已经完全正确。

## 3. 逐请求轨迹与具体差异

下文 `#n` 指该侧**主请求编号**；完整表使用实际 HTTP 编号，辅助独立列出。M/NZ 的 HTTP #22 是 stall sidecar，此后主 #22/#23/#24 对应 HTTP #23/#24/#25。S/NZ 的 HTTP #11/#14 是 verifier，主 #11/#12 对应 HTTP #12/#13。

| 任务 | InfCodeX 完整逐请求表 | NZ 完整逐请求表 |
|---|---|---|
| M | [3 次主请求](M/infcodex/trajectory.md) | [24 主 + 1 辅](M/nzcoder/trajectory.md) |
| Q | [9 次主请求](Q/infcodex/trajectory.md) | [16 主 + 1 辅](Q/nzcoder/trajectory.md) |
| S | [3 次主请求](S/infcodex/trajectory.md) | [12 主 + 2 辅](S/nzcoder/trajectory.md) |

每行记录预算、finish_reason、工具、执行/dispatch/command failure、请求前文件版本、NZ generations/ledger 和 Provider usage。完整参数及模型该次实际可见的工具历史在同目录 `causal-table.json`；原始请求/响应在 `provider-requests.jsonl` / `provider-responses.jsonl`。InfCodeX 未暴露的 NZ 式 ledger 不补造，标为 N/A。

### M：金额计算、五个调用方、兼容旧入口

| 时点 | InfCodeX | NZ |
|---|---|---|
| 初步调查 | #1 读取全部 10 文件；#2 成功执行仓库、Python/pytest 环境检查 | #1–4 调查；两次复合 Bash 被拒，使用逐文件读取继续 |
| 实现 | #3 输出 5 个 `todo_create`，规划 API、调用方、测试、文档、验证 | #5 建立 quote.py；#6–9 连续调整类型/辅助实现，模型历史中可见 basedpyright 警告；#11 批写因 overwrite=false 未覆盖已有文件；#14 成功迁移五个调用方与文档 |
| 实际测试/修复 | 无新实现，无新需求验证 | #16 真正 pytest 失败：6 failed / 14 passed；#17 修复金额缩放；#18 管道输出 20 passed，但不能作为可信声明命令通过 |
| 后续 | #3 的 finish_reason 仍为 tool_calls，随后 Runtime `turn.completed` / `success=true`，无文件修改 | #20 静态验证；#21 重读时自然触发 stall sidecar，判断未卡住；自动 targeted pytest 实际执行但导入 billing 失败，独立记录为 command failure；#22 精确 `python -m pytest -q tests` 真实 20 passed |
| 结束 | 提前成功报告，独立验收失败 | #24 stop 终答；Runtime `max_turns / hard_requirements_unresolved`；最终独立验收 19 pass |

NZ 的关键阻塞已经不是“测试事实没有进入 Runtime”：最终 mutation / acceptance / verification generation 都为 9，contract_passed=true。初始 requirement R1 却把 `REQUIREMENTS.md` 列为必需修改产物；用户原文要求实现该文档描述的迁移，并未要求改写规格文档。该文件未改，R1 始终 pending；最终 R3 兼容性仍 candidate，semantic review generation=-1，没有 semantic acceptance。这个错误产物义务是直接可见的 completion 阻塞；去掉它以后能否在原预算正常完成，**没有做反事实重跑，不能保证**。

NZ 中途确有一次 coding failure（金额缩放），随后真实测试驱动修复；最终代码正确性与未结束必须分开。权限拒绝、overwrite 参数失败、LSP 相关修改都消耗轨迹，但不能把它们统称“测试失败”，也不能声称任何单项独立导致最终 max_turns。

### Q：有界并发、首错误/取消、等待在途任务

| 时点 | InfCodeX | NZ |
|---|---|---|
| 实现 | #1 读完整上下文；#2 写 pool/batch；#3 写测试 | #1–5 调查，包括被拒的 ls/cat 与 node --version；#6 写实现；#7 写测试；#8 修正自写测试并更新 README |
| 测试与修复 | #4 实际 Node：36 pass / 1 fail；#5 修改错误的自写断言；#6 Node 37 pass | #9 Node 经 tail 显示 27 pass；#10 verify_changed_files，另一个 grep 管道命令被拒；#11 循环命令被拒；#12 再次 tail 显示 27 pass |
| 收尾 | #7 文档；#8 核对 exports，再跑 Node 和本地 npm test；#9 stop 正常结束 | #13–15 三次 review_run_evidence 参数尝试，分别出现缺 modified_files、缺 verification_results，最终 approved；#16 stop |
| Runtime completion | run.result success=true | Runtime 执行**精确声明 Node 命令**真实通过，辅助 verifier accept，completed |
| 独立验收 | 7 pass | 7 pass |

InfCodeX #5 调整的是同步 worker 抛错后的 started 断言：观察到首个同步失败应立即停止调度，其改为只启动 index 0 符合公开要求，不视为掩盖实现缺陷。两侧最终实现都通过相同的容量补位、有序结果、错误身份、abort 转交、停止调度并 drain、监听器清理检查。

NZ 的尾部管道结果没有被直接当作声明测试成功；最后 production completion path 自行运行精确命令，可信结果进入当前 workspace/run/version。最终 G=6、verification generation=6、semantic generation=6；acceptance mutation generation=5，因为最后一代是文档变化。应按实现的绑定语义解释，不能机械要求所有 generation 数值相同。需求全部 satisfied，无 unresolved。

这题 InfCodeX 更流畅（9 主、113.20s 对 16 主+1辅、192.91s），NZ 总 token 更少（289,659 对 404,577）。NZ 的三次 review 参数尝试是明确的额外请求开销，但没有阻止本题完成，不能单凭它确认独立 fatal root cause。

### S：v1/v2 迁移、revision 冲突、原子写入与损坏保护

| 时点 | InfCodeX | NZ |
|---|---|---|
| 调查 | #1 读 6 文件；#2 baseline 3 pass、Git/版本检查、glob | #1–3 读需求和全部实现；首条复合 Bash 被拒 |
| 实现 | #3 输出 5 个 todo_create 计划，随后 Runtime 成功结束 | #4 批写 store、两个调用方、README、两份新测试；#5 修正自写测试 |
| 测试 | 只有原基线通过 | #6 管道输出 83 pass；#7 静态 verifier；#8–9 review 参数尝试；#10 精确 pytest 真实 83 pass |
| 首次 completion 候选 | 没有实现却成功结束 | #10 后 early tool completion 候选，contract passed、无 unresolved；辅助 verifier **revise**：补报实际测试结果和剩余限制，不要求继续改文件 |
| 后续/终答 | 无后续主请求 | #11 再调用 review；#12 自然语言 final 报告结果/限制；第二次 verifier **accept**，completed |
| 独立验收 | 1 fail / 18 setup errors | 19 pass |

NZ 的修订发生在交付说明，verifier revise 之后没有文件修改。最终 G/acceptance/VG/semantic 都为 2，精确测试通过、需求 satisfied，terminal reason=completion_gate_satisfied。这是辅助审查实际影响终止边界的可见例子：测试绿并没有直接越过用户要求的结果/限制报告。

最终实现具备同目录临时文件、flush/fsync、replace、冲突检查、损坏文件拒绝和失败清理；但没有跨进程锁，revision 读取与 replace 存在 TOCTOU，也没有目录 fsync 或崩溃遗留临时文件恢复。公开要求允许并要求说明并发边界，最终 README/答复有说明；通过验收不应被描述为完整多进程事务存储。

## 4. 第一个有证据支持的运行时瓶颈

**InfCodeX：纯待办工具批次后的提前完成。** M/S 两次真实响应均为有效 todo_create 调用、finish_reason=tool_calls，随后运行时 success=true、limitReached=false，最终任务文件完全未变。不能将它描述为“模型 stop 后完成”，也不能把创建计划等同于交付。

只读源码检查给出一致的机制解释：`event-emitter.ts:325` 将 todo 系列标为不可见；`tool-dispatch.ts:648` 仅将可见工具加入 toolResults；`run-substrate.ts:1776` 在 toolResults.length===0 时按 hadToolCalls=false 结算并可返回 success。这与两条真实轨迹吻合。证据与源文件 SHA-256 见 [source-corroboration.json](source-corroboration.json)。这是有源码支持的强解释，但内部 todoStore 是否真正创建成功没有公开 tool.result，报告不冒充观测到了该内部状态，也没有新增失败回归来证明修复方案。

**NZ：M 的要求文档被误当修改义务。** R1 的 expected_artifacts、未改 REQUIREMENTS、最终 pending 与 terminal reason 均为运行时直接事实。可信测试已经存在，阻塞仍在 requirement completion；R3 candidate/semantic 未接受也保留原样。这里值得下一轮先建立最小失败回归，再评估最小修复。本批只比较和取证，不改 Core。

**NZ：额外请求开销。** Q/S 的 review_run_evidence 参数试探、严格 Bash 权限拒绝，以及 M 类型警告相关连续修改均真实发生；但 Q/S 仍能完成。仅据本批不能将 schema 或权限单独判为所有失败的根因，也不据此放宽权限/削弱证据门槛。

## 5. Usage：仅 Provider 原字段

| 侧 / purpose | 实际请求 | prompt_tokens | completion_tokens | total_tokens | prompt_cache_hit_tokens | prompt_cache_miss_tokens |
|---|---:|---:|---:|---:|---:|---:|
| NZ 主 | 52 | 877,582 | 95,399 | 972,981 | 355,328 | 522,254 |
| NZ 辅 | 4 | 26,690 | 936 | 27,626 | 2,816 | 23,874 |
| InfCodeX 主 | 15 | 503,566 | 38,507 | 542,073 | 67,456 | 436,110 |
| InfCodeX 辅 | 0 | 0 | 0 | 0 | 0 | 0 |

实际 **67 主 + 4 辅 = 71 次**，合计 **1,542,680 total_tokens**。NZ 辅助为 M 的 1 次 stall_sidecar、Q 的 1 次 verifier、S 的 2 次 verifier。所有请求 HTTP 200 且 usage 可用，未自动重跑样本，没有因网络错误重试的额外请求。辅助未隐藏在主预算之外；每次明列并受独立上限约束。

逐请求 usage 保存在各运行 `usage.json`，保留 Provider 字段。cache hit/miss 已包含于 prompt，不再次相加。completion_tokens 按 Provider 定义记录，不等同于可见自然语言长度。供应商未暴露真实账单：**cost unknown**；不从 runtime cost=0 推断免费，不推算美元。InfCodeX 两个失败任务提前退出，所以其整批总 token 较低不能解读为等质量更省。

## 6. 证据完整性和局限

[integrity-audit.json](integrity-audit.json) 核对同题初态哈希与 Git 基线、冻结任务/验收清单、全部最终文件哈希、请求预算/响应/usage 和源码工作树。[access-audit.json](access-audit.json) 保存模型实际工具路径与 Bash 命令；检查未发现读取另一侧工作区或独立验收、安装依赖、访问在线评价。该检查是轨迹审计，不是全系统 syscall 跟踪；目录/HOME 隔离不等于文件系统不可越界。

`docs/evidence/paid-comparison-2026-09-16` 的 63 个已跟踪文件逐字节与 HEAD 对照通过。生产源码及已跟踪工作树保持干净，没有提交或回退。新报告、采集器和证据位于现有 gitignore 忽略的独立目录，尚未提交。

采集副本去除 reasoning_content、private_reasoning、provider_extra；structured privacy audit 未发现这些字段，未复制私有推理。主请求 live payload 未因采集脱敏而改写。辅助完成可能更新最新 HTTP 计数：例如 M/NZ 主 #21 read_file 的结果在辅助 HTTP #22 后记录；离线表按工具名/完整参数关联回原请求，同时保留 recorded_capture_request_id，避免误写成辅助模型调用 read_file。runtime-owned targeted verifier 另标，不归为辅助模型工具。

每个运行保存 provider 请求/响应、runtime/权限/工具结果、文件版本、最终 diff/hash、冻结验收、usage；NZ 额外保存完整 RunEvidence、state/contract/generation 和 terminal boundary。InfCodeX 没有公开的内部状态标为未知，不以 NZ 的状态机字段替代。

## 7. 评价与结论强度

- **强证据：** 在这批三个题目中，NZ 三份最终实现通过冻结验收，Q/S 正常结束；InfCodeX 只有 Q 实现并通过验收，M/S 原文件未动就报告成功。就本批交付效果，偏向 NZ。NZ/M 仍是“正确代码但未完成”的实际产品问题，不能计为完整成功。
- **强证据：** Q 两侧功能检查均通过，InfCodeX 请求和耗时较少，NZ 总 token 较少。没有统一的“效率全面胜者”。
- **中等至强机制证据：** InfCodeX 隐藏 todo 结果与空 toolResults completion 分支解释两次提前终止；NZ/M 的 REQUIREMENTS 产物误判直接反映在 ledger 和终止原因。没有受控反事实修复 replay，不能承诺修复后的轨迹。
- **证据不足：** 总体成功率、复杂真实大仓库表现、美元成本、模型能力优劣、排除权限/输出限制之后纯 Core 的效果差异。单批样本不能支持这些推广。

已完成授权范围内六次运行及独立验收，至此停止付费调用与实现变更。
