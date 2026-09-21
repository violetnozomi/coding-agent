# Core 离线后续修复

基线 HEAD/origin/main：`e9e24913e419c1dd12ef2e60b745d4422ec6da2a`。本轮承接已有未提交的 [authoritative task-reference 修复](../task-reference-retention-2026-09-20/README.md)，根据用户“能处理的问题都给我处理了”继续处理可独立复现的问题。开始状态保存在 `starting-worktree.patch`，最终完整工作树补丁及源码 hash 另存。本目录开始于 9 月 20 日，收尾于 9 月 21 日。

**0 paid model requests**。没有真实 Provider、辅助模型或 embedding 调用。pytest 经现有 seccomp launcher 禁止 IPv4/IPv6 socket；生产链测试只在 Provider 边界使用本地受控替身，文件工具、subprocess、Runtime、requirement ledger、verification 与 semantic hook 保持真实。没有推断费用、成功率或真实模型请求数改善。

## 已完成的修复与因果依据

| 问题 | 错误产生点 | 修改与保留的边界 |
|---|---|---|
| Reactive compaction 丢失 overflow 标志 | Runner 选择 Context 的单参数 compact，adapter 调用 `_compact_messages` 时 overflow 默认为 false | Context 新增可选 `compact_overflow` capability，生产 adapter 显式绑定 true；原 compact 和旧自定义 Context 兼容，共享三次压缩预算不变 |
| review 工具要求模型复制内部账本 | schema 要求 evidence，缺省调用失败，模型提供的对象成为分析输入 | 模型工具改为无参数，通过 permission-checked ToolExecutor 的 scoped callback 读取当前 Agent 的真实 RunEvidence/RuntimeState；复用已有 deterministic reviewer、CompletionGate，不新建 Reviewer，不改变 semantic acceptance |
| review 与未提交写批次交错 | 同批次写事实尚未落账时，summary 可能指向旧状态 | 活跃写事务返回 `pending_tool_batch`，待批次结算后再读；不根据尚未确定的账本宣称通过 |
| 无参数 review 被 L2 误判重复探索 | 不同 workspace generation 的 review 输入均为 `{}`，L2 只看到相同工具签名 | 加入现有 deterministic closure tool 豁免，与 diff_status/verify_changed_files 一致；本地连续重复调用保护仍触发，未关闭全局 stall detection |
| Bootstrap 与共享 scope 在换行处不一致 | bootstrap 把换行压成空格，下一行普通文件继承上一行 follow 的 task_spec 角色 | 保留原始 clause 换行再调用共享 classifier；无文件名特判 |
| 安全架构 inventory 两项过时 | 旧清单仍记三处 reference probe；未列入 policy 内部 TypeError 兼容判断 | 独立原基线复现后核对实际源码，更新两处审核清单。没有放宽公开异常内容、进程执行或权限策略 |

review 的原 Python 函数 `review_run_evidence(evidence, runtime, task_mode)` 保留供离线调用；模型工具没有 authority-bearing 参数。旧参数经现有 dispatch 丢弃，不覆盖 Runtime 事实。没有绑定 Runtime 时返回 unavailable；不同 executor/线程隔离，permission denied 不调用 callback。review 仅提供建议，不能设置 completed、关闭 requirement 或替代 semantic review。

前一阶段的核心 invariant 保留：genuine user instruction → shared path-role classification → bootstrap 安全捕获原始规范 → RuntimeState → 独立 verifier reference section。spec 后续修改不能替换原件；相同 hash 的 canonical 完整读取才标 observed；revise 是审查发现，不是新用户 authority。TaskContract 的 expected_artifacts 仍只表示真实 mutation obligation。

## 红灯、绿灯与中间发现

完整逐命令记录：[`commands.jsonl`](commands.jsonl)，包含实际 argv、cwd、exit code、耗时和原始日志文件。各组有重叠，不能相加作为独立测试总数。

| 阶段 | 实际结果 | 证据 |
|---|---|---|
| 原 compaction 两项 | 2 failed | compaction-before.txt |
| compaction 聚焦修复后 | 6 passed | compaction-after.txt |
| review 接口初始回归 | 4 failed | review-before.txt |
| review 写批次边界 | 1 failed / 1 passed | review-edges-before.txt |
| bootstrap 换行 scope | 1 failed | reference-scope-before.txt |
| reference / requirement / bootstrap | 116 passed | reference-scope-after.txt |
| architecture 原工作树 / 独立 e9e2491 | 两边均 2 failed / 20 passed | architecture.txt / architecture-baseline.txt |
| architecture + context budget 更新后 | 59 passed | architecture-after.txt |
| 扩大 runtime / verification / security / loop / recovery / subagent 回归 | 1513 passed / 4 skipped | broad.txt |
| 强化终态断言发现 review L2 交互 | 1 failed / 1 passed | review-stall-before.txt |
| deterministic closure 本地保护红灯 | 1 failed / 1 passed | review-policy-before.txt |
| 最终 review / stall / policy / context 聚焦 | 51 passed | review-final.txt |
| 最终 loop / architecture / context budget | 157 passed | closure-final.txt |
| 最终所有改动 Python 文件 Ruff | passed | lint-final.txt |

中间 fixture 修正也保留日志：简单任务在既有 early completion 边界提前成功，未执行脚本中的第三次 review，因此早期断言失败。最终通过现有 semantic hook 的受控 revise→accept 延长轨迹，没有改 Runner 终态逻辑。随后加强 `completed`、两次 semantic 请求、无 stall sidecar verdict 的断言，才暴露 L2 交互；此前 broad 的通过不作为这一新增断言的证明，修复由最终 51 项聚焦回归覆盖。

可复跑命令示例（完整 argv 以 commands.jsonl 为准）：

```bash
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_task_reference_evidence.py tests/runtime/test_reference_scope_followups.py
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/runtime/test_runtime_review_interface.py tests/runtime/tool_runtime/test_focused_policy.py tests/test_stall_detector.py tests/test_stall_sidecar.py tests/test_reviewer.py tests/runtime/core/test_context_execution_context.py
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q tests/test_loop_fake.py tests/architecture tests/test_context_budget.py
```

## 受控生产回放

[`replays/`](replays/) 保存模型边界请求、Runtime events、权限、逐请求前状态、最终 ledger、真实工具结果、workspace diff、最终文件与 SHA-256。Provider 替身的 semantic accept 只用于验证已有链路，不证明真实 verifier 会作相同判断。

- `runtime-review-False`：无参数调用，修改前 requirement 未完成，修改后验证未完成，真实测试通过后同 generation，semantic revise→accept，最终 completed。
- `runtime-review-True`：额外伪造 evidence/runtime/task_mode 参数仍只能读取真实 Runtime，最终经过实际修改/测试/semantic 路径 completed。
- `review-missing-caller-stale`：漏改 caller 且旧验证不能复用，最终 max_turns。这里是不允许提前结束的成功反例，未重命名为 completed。
- `review-unsettled-batch`：写入同批次返回 pending_tool_batch，随后按已有真实结算/验证路径 completed。

## 修改文件

本轮追加到已有 reference 修复的生产修改：

- `runtime/core/context.py`、`runtime/adapters/context.py`、`runtime/execution/runner.py`：overflow compaction 能力传递。
- `intelligence/reviewer.py`、`runtime/execution/tool_executor.py`、`runtime/execution/loop.py`、`runtime/conversation/prompt.py`：无参数 Runtime-owned review 与只读建议。
- `runtime/tool_runtime/policy.py`：deterministic closure tool 的既有 L2 豁免。
- `intelligence/bootstrap_artifacts.py`：保留换行作用域。

新增 `tests/runtime/test_runtime_review_interface.py` 和 `test_reference_scope_followups.py`；扩展 context adapter、focused policy、reviewer、smoke 测试及 architecture inventory。最终 `source-hashes.json` 列出相对 HEAD 的全部源码/测试文件，包含前一阶段 retained-reference 实现；`implementation.patch` 同样是完整累计差异，不将其误称全部为本轮新代码。

## 限制与交付状态

已经修复本次发现且有失败回归的确定性问题，不能据此宣称整个 Core 没有任何问题。没有执行仓库全部测试；已执行上述相关大组合与最终变更聚焦回归。

继续保留安全证据边界：原规范最多 4 份、单份 8 KiB、总计 16 KiB；超预算 complete=false 并标记原因，不伪造完整规范。缺失旧 checkpoint 不从 Agent 修改后的文件补造原件。路径分类仍是保守句法解析，不覆盖任意自然语言、带空格/无扩展名路径；多次 partial read 不冒充单次完整观察。真实模型自主轨迹改善尚未做付费复测。

前一阶段及历史实验目录保持原样，完整性检查见 `integrity-audit.json`。本目录记录原因、设计取舍、失败与通过结果，未保存 private reasoning_content。当前为未提交、未推送的本地工作树；evidence 目录受仓库 ignore 规则影响，未来提交时需要显式纳入，不能仅依赖 `git add .`。
