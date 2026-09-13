# NZ-Coder 与 InfCodeX 的差距分析

_基于 2026-09-13 公开代码和本地 SWE 运行证据的阶段性分析_

---

## 📋 结论先行

此前版本把两个不同项目混为一谈：InfCodeX（KodaX CLI）与 Tokfinity/InfCode
论文/评测实现不是同一个代码库。5 个 Generator、Selector、140/200 轮以及论文中的
Verified 数字不能作为 InfCodeX 的证据；`scaleapi/SWE-bench_Pro-os` 是评测 harness，
也不是 InfCodeX 的成绩报告。

本次固定核对的 InfCodeX 源码是 `d3a812379b589597347f5be12d5b68477e577f02`。
没有找到该项目针对 SWE-bench Pro 的公开可核验成绩，因此不能计算与 NZ-Coder 的
公开分数差。NZ-Coder 的本地失败证据仍表明单 Agent 在仓库定位、上下文预算和验证
收敛上存在问题，但不能由此推出 InfCodeX 的优越幅度。

本地证据也显示，NZ-Coder 的失败不是“模型完全不会修”：16 次历史轨迹中，4 次
完成运行都在 7–17 turns 内完成单文件修改；失败组平均 26.7 turns、16.5 次
compaction，且有 86 个 failure-repair turns。最近的 Ansible Pro 试跑在 20 轮时
没有编辑，提到 80 轮后在第 28 次调用被停止时仍没有编辑。

历史 Lite 聚合为 16 次内部运行（completed 4、risky 6、agent_failed 6），不是
官方 pass@1。Ansible Pro 两次受限运行在编辑前耗尽/停止；原始临时目录目前已不存在，
缺失字段不应被补写成推测。本轮修正提交为
`7972538c9381666413dcef809f1d3696fc3398f0`，定向回归为 493 项；不将测试通过数
当作 SWE-bench Pro 通过率。

## 🔍 可核查的对照

| 能力 | NZ-Coder 当前证据 | InfCodeX 公开实现 | 对结果的影响 |
| --- | --- | --- | --- |
| 独立循环上限 | 配置默认 500；本次命令强制 20，后续试跑 80 | `MAX_TOOL_LOOP_ITERATIONS=20` | 单纯提高上限不能保证收敛 |
| SWE 任务编排 | 当前直接单 Agent Core | 当前 README 描述 V2 Worker single-loop + out-of-band Sidecar；旧 V1 chain 已退役 | 没有 5-generator 证据，不能据此比较 |
| 并发 | 工具读操作可并行，但本次模型只发单个搜索/读取批次 | README 的 Worker/子任务隔离与批处理并不等于同一工作区并发写入 | 不能用批处理并发推断单题质量 |
| 仓库浏览 | 已有 repo graph/code index 和 `repo_map` 工具，但模型本次主要使用 `grep_search`、`read_file`、`list_directory` | `builtin-agents.ts:21-75` 提供 `repo_overview`、`module_context`、`symbol_context`、`process_context`、`impact_estimate`、relationship/LSP 工具，明确结构化工具优先 | NZ 的缺口是实际使用率/提示编排，不是没有结构化工具 |
| 上下文 | 32k 配置、约 28k usable；本次出现 token estimate 31,366，Provider `finish_reason=length` | 有摘要压缩、物理容量和结果截断规则 | NZ 在尚未编辑前承受上下文压力，但两者策略不能简单归因 |
| 压缩 | NZ 有 `context_evidence_projected`/micro-compaction，但历史失败组平均 16.5 次 | `packages/agent/src/primitives/compaction.ts:87-190` 默认 80% 阈值、保留最近 10 条并生成摘要 | 两者都有压缩，不能写成 InfCodeX 没有摘要 |
| 验证 | 有静态、targeted、regression 规划和 sidecar；失败时可能进入重复 repair | 当前默认 Sidecar Verifier 返回 accept/revise/blocked | 两者都有验证闭环；需要比较实际终止轨迹 |
| 预算/停止 | 以 turn、context、tool policy 控制；Pro 试跑权限确认也占用墙钟时间 | standalone Runner 20；托管安全上限 500，另有 budget controller | 当前主要浪费在编辑前调查和失败修复循环；上限不是质量保证 |

InfCodeX 的公开代码和 README 描述的是 KodaX 的通用 Agent/托管路径；本文没有发现
可核验的 InfCodeX SWE-bench Pro 成绩。论文/评测仓库中属于 InfCode 的数字必须单独
标注项目名，不能转写到 InfCodeX。

## 📊 NZ-Coder 失败为何反复出现

### 调查阶段没有硬收敛点

Ansible 试跑的 29 个工具调用中有 15 次 `read_file`、6 次 `grep_search`、6 次
`bash`，直到第 25 个模型回合仍未调用 `edit_file`。这不是仓库看不见：模型已经
读到了 `dataclasses.py` 和 `collection_loader` 代码。问题是没有在“已找到候选定义、
已有失败例子”后强制转入最小修改/验证阶段。

### 上下文预算在编辑前被耗尽

该次运行记录了多次 `context_preflight_over_soft`；请求估算最高 31,366 tokens，
而 usable input 为 28,000。第 21、22 回合出现 `finish_reason=length`。compaction
降低了部分历史长度，却没有消除重复搜索和工具输出累积，所以增加轮数只是让它在
更高成本下继续调查。

口径修正：`finish_reason=length` 本身表示 Provider 输出达到上限，并不等同于服务端
返回了 context-overflow。当前证据能确认的是请求估算多次超过 usable input、并发生了
三次自动 compaction；历史 trace 不足以把最后的 `length` 归因成某个服务端 HTTP 错误。
因此后续改进将分别验证“请求预算超限”和“输出上限续接”，不再把两者混写成单一故障。

### 成功轨迹具有清晰形状

现有成功样本全部是：先复现 → 1 次源文件编辑 → 立即运行目标测试 → 静态检查/最终
回答；没有 failure-repair turns，没有测试文件修改。失败样本则平均包含大量
`failure_repair`、`verification_needed`、环境阻断或宽泛搜索。当前质量门应优先识别
这两种轨迹，而不是只增加 token 或 turn。

### 工具策略和权限开销混在一起

历史 417 次工具调用中有 48 次 `dispatch_failed`（其中 40 次 Bash）和 27 次
`command_failed`。最近 Pro 运行的搜索命令本身成功，但每条 Bash 仍弹出人工批准，
使“模型思考时间”和“环境等待时间”混杂。权限安全不能关闭，但应把权限等待从模型
回合预算和诊断中单独计量。

## 🎯 按顺序完善，而不是继续盲调轮数

### 收敛门：已接入，但收益尚未证实（2026-09-13）

已在正常交互式运行的工具策略边界加入一个收敛门：当任务被识别为
`bugfix`/`feature`/`refactor`/`test`，尚未发生变更，且已经成功读取至少一个源码文件和
一个测试文件时，调查调用达到动态阈值（短运行 12 次，长运行最多 20 次）后，宽泛
搜索/目录探索会被明确拒绝，并返回“先编辑或运行最窄验证”的工具结果。精确
`read_file`/`read_symbol` 保持放行，以便核对已读内容或跟随新关联文件；编辑、diff、
验证和安全策略不受阻断。该门由 `RuntimeState.implementation_gate_active()` 和
`ProductionToolPolicy.strict_progress_rejections()` 共同实现。由于直接读取仍可继续，
这只是防止宽搜空转的提示，不是“已定位”的证明，也没有 SWE 质量收益证据。

回归先在旧实现上得到预期失败，再在修复后通过：

```text
python3 -m pytest -q \
  tests/runtime/tool_runtime/test_focused_policy.py::test_convergence_gate_blocks_more_investigation_after_localization
python3 -m pytest -q tests/runtime/tool_runtime tests/test_loop_fake.py tests/test_runtime_state.py
299 passed in 150.75s
```

因此不能把该项写成已完成的质量改进，也不能宣称解决了上下文压缩、多候选编排或
SWE-bench Pro 通过率。

### 第二项已落地（2026-09-13）

压缩只替换对模型可见的 transcript，不改变已经执行的工具事实或工作区。因此
`AgentLoop._on_context_compacted()` 现在保留 RecoveryState 的重复调用窗口和 stall
sidecar 窗口，不再把 compaction 当成全新任务而清空 doom-loop 证据。工作区发生真实变化
时仍由既有 `workspace_changed` 边界清理；用户明确批准重复调用时仍走原有 reset。
回归 `test_context_compaction_preserves_tool_stall_history` 在旧实现上失败，修复后与
context/recovery/hooks 定向测试共 109 项通过。该修复防止通过反复触发压缩绕过防循环保护，
但不改变 hard context limit，也不声称已消除 Provider `finish_reason=length`；后者仍需
独立的预算分配回归。

### 仓库检索刷新：行为范围有限（2026-09-13）

仓库索引在首轮仍处于 warming 时，后续回合可能已经得到新的候选文件；此前
`_repo_retrieval_block()` 在 turn>1 直接丢弃这些结果，模型只能重新 grep。现在按
`generation + query + strategy + candidate_files` 做一次性可见签名：索引产生新的具体
候选时只注入一次受限 routing block，随后不重复膨胀上下文；没有候选、不同会话或
`tool-only` 行为保持不变。定向回归
`test_repo_retrieval_refreshes_new_candidates_after_first_turn` 覆盖了“首轮无结果、
第二轮索引就绪、第三次不重复注入”的链路，retrieval/service/prompt 共 83 项通过。
该测试使用 fake ready 结果，且默认 `repo_retrieval_strategy="guidance"` 不会主动查询
候选索引；它不能证明真实 warming 或跨 Session 持久可见性。

### 上下文截断：已确认并修复一项确定性缺陷（2026-09-13）

旧 `truncate_text_tokens()` 用 `payload_tokens * 4` 估算字符数，但估算器对中文按字符
计 token。给定 100-token 预算的中文块，旧实现实际估算为 373 tokens；新增回归先在旧
实现失败，修复后改为按同一估算器二分选择前缀/后缀，并验证 marker 也在预算内。该修复
只保证本地动态块不超过声明预算，不等同于 Provider 端到端上下文通过。

### 1. 调查→编辑收敛门（优先级 P0，待反例审计）

已新增确定性反例：已读源码/测试后仍需精确重读时，调用不会被静默拒绝。后续若要
限制直接读取，必须先证明轮换路径导致的真实空转，再设计独立预算；不把拒绝次数下降
当作成功指标。

### 2. 修正上下文预算的分配（P0）

把 compaction 前后的物理 token、工具输出占用和 Provider `length` 错误作为独立
指标。优先压缩重复目录/搜索结果，保留最近源码片段和任务约束；当 usable input
不足时提前停止调查，而不是连续发送必然超窗的请求。验收条件：同一配置下不再出现
请求估算大于 usable input；首个 `length` 错误必须触发一次有界重整，不得重复原请求。

### 3. 让验证成功后立即收敛（P1）

复用现有 verification generation：目标测试和静态检查都通过、diff 低风险时，直接
进入 final answer，禁止再次搜索同一符号。若测试失败，只允许一次有上下文的修复轮，
然后报告失败事实。验收条件：成功样本保持 1 次编辑、无测试改动；失败不会形成多轮
重复 repair。

### 4. 再评估是否需要托管多候选（P1）

只有单 Agent 收敛门和上下文修复有离线证据后，才考虑实现受控的多候选/Selector。
不能直接复制未经证实的 5 倍并发：它会放大费用、工作区隔离和结果选择复杂度，且
InfCodeX 自身没有公开 Pro 证据。候选实验应使用同一题、同一模型、固定费用目标，并
单独记录每个候选。

### 5. 最后调整默认轮数（P2）

NZ-Coder 默认 500 已足够作为上限；Benchmark profile 可以从 20 改为 40–60，再依据
收敛指标决定是否提高。轮数是安全上限，不是“让模型继续漫游”的质量策略。

## 🧪 下一轮验证矩阵

| 实验 | 固定项 | 只改变 | 成功标准 |
| --- | --- | --- | --- |
| A | 同一 Pro/模型/题目 | 20 vs 40 turns | 首次编辑回合、Provider length、费用 |
| B | 同一配置 | 上下文去重/预算门 | 不超 usable input，至少完成一次编辑 |
| C | 同一配置 | 调查收敛门 | 目标定位后 10 回合内编辑 |
| D | 离线受控 Provider | 失败测试→修复→再测 | 无重复工具调用，最终状态真实 |

A/B/C 必须先使用受控或单题授权；不应同时改变模型、提示、工具和并发，否则无法
判断是哪一项有效。真实 Pro 新调用需要单独授权，不能把历史 Lite 或当前手动目标
当成自动预算。

## 🔗 证据与参考

- NZ-Coder 本地运行证据：`.nz-coder-runs/swe-pro-single-20260913/` 和
  `.nz-coder-runs/swe-core-visible-20260912-aqlOPO/`
- NZ-Coder 历史聚合轨迹：`.nz-coder-runs/swe-*/predictions.report.json`、
  `predictions-trajs/*.jsonl`
- [InfCodeX README](https://raw.githubusercontent.com/Tokfinity/InfCodeX/main/README.md)
- [InfCodeX Runner](https://github.com/Tokfinity/InfCodeX/blob/main/packages/agent/src/primitives/runner.ts)
- [InfCodeX loop limit](https://github.com/Tokfinity/InfCodeX/blob/main/packages/agent/src/primitives/runner-tool-loop.ts)
- [InfCodeX Pro evaluation repository](https://github.com/scaleapi/SWE-bench_Pro-os)
- [Scale SWE-bench Pro dataset](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)

[^1]: Scale AI, “SWE-bench Pro” dataset documentation: https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro
