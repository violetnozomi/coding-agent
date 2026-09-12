# NZ-Coder 与 InfCodeX 的差距分析

_基于 2026-09-13 公开代码和本地 SWE 运行证据的阶段性分析_

---

## 📋 结论先行

20 轮不是根因。InfCodeX 的独立 `Runner` 默认同样是 20 次 tool-loop；它在
SWE-bench 场景使用的是多候选托管流程（5 个 Generator，每个最多 140 轮，随后
Selector 最多 200 轮）。NZ-Coder 这次 Pro 试跑则是单一 Agent、单一工作区、单一
20/80 轮预算。因此真正差距是**任务编排和收敛机制**，而不是把一个常数从 20 改成
80。

本地证据也显示，NZ-Coder 的失败不是“模型完全不会修”：16 次历史轨迹中，4 次
完成运行都在 7–17 turns 内完成单文件修改；失败组平均 26.7 turns、16.5 次
compaction，且有 86 个 failure-repair turns。最近的 Ansible Pro 试跑在 20 轮时
没有编辑，提到 80 轮后在第 28 次调用被停止时仍没有编辑。

## 🔍 可核查的对照

| 能力 | NZ-Coder 当前证据 | InfCodeX 公开实现 | 对结果的影响 |
| --- | --- | --- | --- |
| 独立循环上限 | 配置默认 500；本次命令强制 20，后续试跑 80 | `MAX_TOOL_LOOP_ITERATIONS=20` | 单纯提高上限不能保证收敛 |
| SWE 任务编排 | 当前直接单 Agent Core | 5 个独立 Generator + 1 个 Selector；Generator 140、Selector 200 | 为长任务提供额外候选和失败隔离 |
| 并发 | 工具读操作可并行，但本次模型只发单个搜索/读取批次 | Generator 任务并发 5；批处理 `--parallel=20` 是跨题并发 | InfCodeX 的并发主要扩大候选，不是同一工作区并发写入 |
| 仓库浏览 | 已有 repo graph/code index 和 `repo_map` 工具，但模型本次主要使用 `grep_search`、`read_file`、`list_directory` | 主要是 `rg` 搜索和行号编辑，无 AST map | NZ 有能力但没有稳定地在首轮使用，模型搜索成本高 |
| 上下文 | 32k 配置、约 28k usable；本次出现 token estimate 31,366，Provider `finish_reason=length` | 无摘要/滑窗；工具结果截断约 16k，但默认模型/配置上下文更大 | NZ 在尚未编辑前就因上下文压力丢失有效轮次 |
| 压缩 | NZ 有 `context_evidence_projected`/micro-compaction，但历史失败组平均 16.5 次 | InfCodeX 没有摘要，依赖较大上下文和缓存 | NZ 压缩发生得频繁，却没有把任务推进到编辑阶段 |
| 验证 | 有静态、targeted、regression 规划和 sidecar；失败时可能进入重复 repair | Prompt 要求复现/回归/submit；Selector 选择候选 | NZ 的验证机制强，但需要“编辑后立即验证”的停机策略 |
| 预算/停止 | 以 turn、context、tool policy 控制；Pro 试跑权限确认也占用墙钟时间 | 生成器有 cost limit/retry 配置；托管链共享更高工作预算 | 当前主要浪费在编辑前调查和失败修复循环 |

InfCodeX 的公开资料只声明 SWE-bench Lite/Verified；没有证据证明它支持
Scale SWE-bench Pro，因此不能把其 Lite 分数直接与本项目 Pro 试跑比较。其公开代码
和 README 也存在叙述与实现不完全一致之处（例如论文称双 Generator，代码实际是
5 个并发的单一 `PatchGenerator` 加 Selector）；这里以代码为准。

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

### 1. 先做调查→编辑收敛门（优先级 P0）

在不改变权限和文件安全的前提下，为每次运行记录只读调查次数、是否已发现目标
符号/测试、首次编辑回合。达到“已有候选定义 + 已有复现线索”后，如果模型继续只读
调用，注入一次短诊断并要求选择：编辑、运行最小复现，或明确说明信息不足。验收条件：
同类任务首次 `edit_file` 不再晚于 10 个有效调查回合；没有目标定位时不得强迫编辑。

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
不能直接复制 InfCodeX 的 5 倍并发：它会放大费用、工作区隔离和结果选择复杂度，且
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
