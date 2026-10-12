# 实际命令、结果与缺项

所有模型边界均为受控替身。启动器移除API凭据，并通过sitecustomize阻断非本地Python TCP；新增Runner夹具额外禁止socket.connect。实际文件工具与Node测试进程没有mock。测试usage不用于价格、token效率或自主能力评价。

## RED：未含修复的A

以`git archive 99eee2967ba8ff4dbfb17bf241e382b8fa022f5a nz_coder tests pyproject.toml`生成隔离副本，只复制本轮回归及其旧记录夹具，未复制.env，未回退主分支。以下相同命令在A副本执行。

```text
python -m pytest -q tests/runtime/test_transaction_owned_edit_runtime.py -k 'same_batch_exact or failed_second or cancel_after or committed_batches'
2 failed, 2 passed, 6 deselected in 3.67s
```

两项失败为test_same_batch_exact_edits_commit_and_reach_real_node_verification、test_failed_second_edit_rolls_back_then_same_plan_can_recover：后续编辑误STALE导致回滚，预期修改文件实际恢复初态。取消/跨已提交批次是已有正确行为，不算新增能力。[完整断言](baseline-red-stdio.txt)、[启动记录](baseline-red-launch.json)。这个初次权威隔离检查发生在录制片段回归加入之前，所以deselected=6；未伪造当前11项的旧计数。

```text
python -m pytest -q tests/runtime/test_transaction_owned_edit_runtime.py -k recorded_b4_09
1 failed, 10 deselected in 1.14s

python -m pytest -q tests/runtime/test_transaction_owned_edit_runtime.py -k external_replacement
2 failed, 9 deselected in 1.98s
```

[录制原片段](baseline-recorded-final.txt)在第二项成功执行断言失败，旧版仍误STALE；第三锚点本身非法的竞争解释保留。[外部改动反例](baseline-external-final.txt)在“外部内容应保留”失败，旧rollback覆盖了原子替换/原地修改。这证明新增所有权约束必须同时守住回滚边界，不能仅放行写入。

## GREEN：修复B

```text
python -m pytest -q -ra tests/runtime/test_transaction_owned_edit_runtime.py
11 passed in 7.40s
```

[最终完整输出](green-final.txt)、[启动配置](green-final-launch.json)。录制片段的第三项真实EDIT_NOT_FOUND并整批回滚才是通过；不会删弱这个断言。单独导出的A/B三请求任务及独立Node复测见[离线记录](../offline-replay/)；B正常completed，A保持max_turns，均0收费请求。

## 受影响的既有回归

下列相关组合已在当前三份生产修改下运行；结果来自控制台记录，未将缺失的原stdout补造成原始文件。新增录制片段加入前新文件是10项，因此44包含这10项；最后11项检查另列，不把重复检查相加冒充更多覆盖。

```text
python -m pytest -q tests/runtime/test_transaction_owned_edit_runtime.py tests/test_edit_recovery.py tests/test_write_files_batch.py tests/security/test_transaction_track.py tests/security/test_transaction_recovery.py
44 passed, 5 skipped in 7.19s
```

5个skip为test_transaction_track.py:263、294（Windows native handle semantics）和test_transaction_recovery.py:189、373、397（Windows junction semantics）。Linux没有执行Windows语义；不称全平台通过。

```text
python -m pytest -q -ra tests/runtime/test_node_verification_runtime.py tests/runtime/test_native_runner.py tests/runtime/tool_runtime tests/runtime/core/test_tool_execution_context.py tests/test_runtime_composition.py tests/test_read_file_parity.py tests/test_permissions.py tests/test_skill_governance.py tests/security --ignore=tests/security/test_transaction_track.py --ignore=tests/security/test_transaction_recovery.py
518 passed, 6 skipped in 18.50s
```

[真实stdout及逐项skip](affected-suite-stdio.txt)、[隔离启动记录](affected-suite-launch.json)。6项分别为：test_pr2_remaining_regressions.py:140/250（Windows CREATE_NEW/fdopen）、test_project_control_snapshot.py:656/679（Windows junction/handle）、test_protocol_and_lock_limits.py:110（Windows reparse）、test_user_runtime_state.py:40（Windows junction）。保留Artifact、Repo、权限、Skill、读取版本、取消、事务与验证回归，没有修改其断言。

## 历史compaction与lint

```text
python -m pytest -q tests/test_loop_fake.py::test_context_overflow_stops_after_three_compaction_attempts tests/test_loop_fake.py::test_pre_send_and_reactive_compactions_share_one_three_attempt_owner
A: 2 passed in 1.49s
B: 2 passed in 1.32s
```

[A原输出](historical-compaction-A.txt)、[B原输出](historical-compaction-B.txt)。旧报告记录的两个失败在当前99基线未复现；本轮未动预算代码、配置或测试，不能归因于事务修复。没有开展进一步预算调查。

```text
ruff check nz_coder/foundation/workspace_file_access.py nz_coder/runtime/execution/tool_executor.py nz_coder/state/transaction.py tests/runtime/test_transaction_owned_edit_runtime.py docs/evidence/swebench-pro-trace-audit-2026-10-12/analyze.py
All checks passed!

ruff check .
E731 tests/evaluation/test_model_relay.py:233
```

[全仓lint原输出](ruff.txt)。同一文件在隔离A运行ruff仍为E731，原文件未修改；不弱化规则、不改无关计费测试。不宣称全仓lint或全仓pytest通过。

过程中一次pytest命令使用不存在的tests/test_tools.py，exit4/no tests ran；随后改成真实相关文件。初次Provider夹具将tuple helper拆错也已排除，这两次均不作为RED或Core失败证据。全部计数以最终明确命令为准。

原pytest失败stdout和core-change.patch保持字节原样：traceback缩进空行及合法diff上下文空行会触发全文件git diff --check的trailing whitespace。仅对作者正文执行空白检查，排除原stdout和字面源码patch；不为了格式检查改写证据。新Core提交本身的diff --check已通过。
