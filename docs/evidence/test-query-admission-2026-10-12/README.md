# 修复测试器名称查询被验证准入误挡

基线 `40b98599db3deca924798c6766b57b5f5c2685f2`，保留前次事务版本修复。本轮只调整现有测试命令分类，不修改权限、验证契约、轮数或完成控制器。没有收费模型请求，也没有重跑 SWE-bench。

## 原因和历史证据

`task_policy.is_broad_test_command/is_exact_test_command` 原来在整个 Shell 字符串中搜索 `pytest/jest` 等名称。已有 diff 后，Bash 便可能把 `ls … | grep jest` 当作全套测试，在启动子进程前拒绝。查询配置文件 `cat jest.config.js` 还会误计为精确测试。相反，`cat tests/test_app.py && pytest tests/` 的文件名会让真正的目录测试被当作精确测试。

已有100题中可明确追溯的只读误挡是四题、五次；不能把所有未执行 Bash 都归为这个原因：

| 任务 | 原始运行时行号 | 原命令中的关键部分 |
|---|---|---|
| b1-05 | [367](../swebench-pro-20-2026-10-11/rest-attempts/05/runtime.jsonl#L367) | `ls /workspace/node_modules/.bin \| grep -i jest` |
| b2-13 | [644](../swebench-pro-20-batch2-2026-10-11/attempts/b2-13/runtime.jsonl#L644) | 查询 package.json 中的 test/jest |
| b3-27 | [409](../swebench-pro-40-batch3-2026-10-11/memory-attempts/b3-27/runtime.jsonl#L409) | 查询 jest/tsc/eslint 可执行文件 |
| b3-27 | [462](../swebench-pro-40-batch3-2026-10-11/memory-attempts/b3-27/runtime.jsonl#L462) | 查询 jest/tsc 可执行文件 |
| b3-28 | [760](../swebench-pro-40-batch3-2026-10-11/memory-attempts/b3-28/runtime.jsonl#L760) | find/grep 查测试配置 |

排除“用户没有授权查询”这一竞争解释：离线回放的权限回调明确允许查询，旧版仍返回 Broad test runner blocked。工具 handler 的 `executed=true` 也不意味着子进程已经运行：旧版记录 `dispatch_failed=true, command_failed=false`，模型收到准入错误。公开 trace 会脱敏错误正文，因此错误原因还要核对真实模型请求。

## 最小修复

唯一生产文件是 [task_policy.py](../../../nz_coder/runtime/agent/task_policy.py)，完整差异见 [core.patch](core.patch)。复用 `verification_command_segments` 的引号感知分段、已有只读命令识别和验证分类：

- 明确的查询/显示片段不贡献测试器名称或文件名。
- 每个真正测试片段分别判断；任何 broad 片段都继续触发原准入限制。
- find 的执行动作不能当成查询；Shell 求值、写入及未识别的消费方保留原保守判断，避免 `echo pytest | sh/xargs` 丢失执行线索。
- 不修改安全只读白名单；识别为查询不会自动给予权限，也不构成测试通过。

不是完整 Shell 执行证明。含未知命令、复杂求值或写入的查询仍可能走保守回退，本轮未建设通用解释器。

## 同条件真实链路

[测试](../../../tests/runtime/test_test_query_admission.py)使用生产 NativeSDKRunner、AgentRunner、ToolExecutor、结果投影、VerificationManager 和真实 Node 子进程，只替换主/辅助 Provider；全部非本地 Python 网络连接被隔离。Provider 只返回工具协议，不写文件或替 Agent 执行命令。

五条历史查询只将 `/workspace` 改为隔离工作区的相对路径。每条经过：读取 → Node 测试真实失败 → 局部编辑 → diff_status 确认非空 diff → 查询 → 同一 Node 测试真实通过。diff_status 是原实现激活准入条件的实际观察步骤，不能省去它后用“未被拦截”证明修复。

| 请求 | 执行 | 旧版 | 新版 |
|---|---|---|---|
| 1 | read_file index.js | 实际读取 | 相同 |
| 2 | node --test literal.test.cjs | 非零、Invalid regular expression | 相同 |
| 3 | edit_file | 提交局部修改，generation=1 | 相同 |
| 4 | diff_status | has_non_empty_diff=true | 相同 |
| 5 | ls node_modules/.bin \| grep -i jest | 子进程未启动，dispatch_failed=true | 真实 exit=0，输出 jest |
| 6 | 模型接收查询结果，再运行 Node 测试 | 看见 Broad test runner blocked；测试通过 | 看见 jest；测试通过 |
| 边界 | 既有完成契约 | completed | completed |

完整 [旧版请求](red/requests.json)、[旧版工具/状态](red/runtime.json)、[新版请求](green/requests.json)、[新版工具/状态](green/runtime.json)、[diff](green/diff-status.txt)及双方 [red](red/query-summary.json)/[green](green/query-summary.json)查询摘要已保存。双方均6个主请求、0个辅助请求；虽然辅助模型边界也被隔离，这个片段并未触发模型语义审查。usage 是测试值，不能用于真实费用或效率比较。双方最终都完成，差异是查询证据能否抵达模型，不是脚本化任务成功率提高。

反例覆盖真正 broad 命令、查询后接目录测试、精确测试后接全套测试、打印/读取的 .py 名称不能缩窄另一测试片段、用户拒绝、同步 ToolRuntime 和未执行测试不冒充通过。查询不会生成 verification_result；当前版本的真实 Node 失败→通过仍走已有验证链。

## 红绿和相关回归

原基线用 `git archive` 在独立目录重建，仅复制相同新增测试；未回退当前工作树。专项命令：

```sh
python -m pytest -q tests/test_task_policy.py tests/runtime/test_test_query_admission.py
```

[旧版](red-tests.txt)：17 failed、20 passed；随后新增的两个空输入兼容反例在旧版另跑均通过。[新版](green-tests.txt)：39 passed。空输入保持原有 false 语义。调试中曾有缺少 diff_status 和依赖公开 trace 错误正文的夹具错误，已纠正；以上红绿是相同最终行为条件，不采用那些试跑作为效果证据。

相关核心、工作流、Skill、安全/事务/文件身份及权限回归的完整 [命令](broader-test-command.json)和 [输出](broader-tests.txt)：1620 passed、11 skipped、5 failed。期间向长时运行的测试进程发送过一次 SIGINT；Runner 吸收取消后组合运行至结束，因此不将该次结果宣称为无干预全绿。五项失败再在原 HEAD 和当前分别独立运行，均复现：[原版](baseline-existing-failures.txt)、[当前](current-existing-failures.txt)：

- `test_frozen_verifier_authority_is_complete_but_not_clause_inventory`：历史审查 prompt 与当前 prompt 不同。
- `test_headless_target_workspace_does_not_inherit_startup_workspace_config`：在本次离线环境下 code=3，而断言期望0；没有据此判断线上行为。
- `test_all_direct_config_reads_are_classified`：现有 SWE 配置访问未进入架构清单。
- `test_all_direct_subprocess_launches_are_classified`：现有 SWE 子进程启动数量与清单不同。
- `test_raw_exception_projection_sites_are_classified`：现有 evaluation/SWE 异常投影数量与清单不同。

未修改或弱化这些测试。11个 skipped 均是 Windows CREATE_NEW、句柄、junction/reparse-point 语义，本机为 Linux。两项旧 compaction fake 用例在本次组合中通过，并非本次五项失败。变动源码 Ruff 和源码/README 的 diff 空白检查通过；原始测试输出保留其本来空白。未运行全仓或 Windows。

[历史证据核验](history-integrity.json)：1094份记录无变化、无缺失；100题评分及补丁原件和已冻结的事务修复 A/B8 设计保持原样。本轮没有新的付费授权，不启动未见任务对照。

结论：已证明该具体准入误判在旧版存在、修复后真实查询可执行并抵达下一次模型请求，权限和验证反例保持有效。真实模型是否减少调查空转、提高任务正确完成率，尚未复测。
