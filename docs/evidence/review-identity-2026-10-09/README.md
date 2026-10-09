# 审查裁决与证据身份校准

起点为 `cf2ff5cf078559e9843c34614318d80c984b4168`，分支 `main`、工作树干净，没有后续增量或用户未提交修改。生产修复最终 SHA `d098054e974a296518c5af771b3eccb9d1814b4e`；证据另行提交，交付 SHA 在最终答复及对应 CI 中列出。没有回退、强推、增加轮次或放宽权限。此阶段修复 NZ-Coder 自身正确性，不将这些设计概括为 InfCodeX 的保证。

上一轮 review.md、probe_results.json 和 review bundle 在当前本机未找到；按请求在生产代码建立反例，没有依赖摘录实现。历史 [Core Runtime job](https://github.com/violetnozomi/coding-agent/actions/runs/36090874867/job/107932844046) 的 SHA 已通过 API 核对，原日志的五项失败见 [historical-ci.txt](historical-ci.txt)。它们与历史两个 compaction fake 失败分别记录。

## 七项台账

| 项目 | 起点事实与本轮状态 | 实际修改与验收 |
|---|---|---|
| C1 Node 隔离 | 仍存在→本轮已修复。历史三项 Node 测试在真正启动前 FileNotFoundError；本机有系统 Node，因此用显式非系统安装路径、缺失配置与仓库同名脚本建立失败回归 | `validate.py::_trusted_node/execute` 清洗前寻找可信安装目录，隔离子进程验证 Node≥18 和 node:test，实际命令使用绝对路径；工作区脚本不入选；缺失返回 environment_blocked。CI 明确准备 Node24并运行隔离执行测试，无 npx 下载、无新增 skip |
| C2 依赖方向 | 仍存在→本轮已修复。TaskContract 两处反向导入 execution.runtime_state，原架构断言失败 | 无状态 `extract_explicit_mutation_operations` 移至既有 `task_policy.py`，TaskContract 与 RuntimeState 共用一份实现；原入口通过 import 保留，不增加白名单 |
| C3 测试污染 | 仍存在→本轮已修复。安全测试写共享临时父目录 outside.md，使导出测试组合失败 | 两个测试分别使用私有 workspace/outside.md；拒绝后已存在哨兵不变、缺失目标不创建、合法导出成功、symlink 被拒绝。正反顺序均 65 passed；不宣称发现新的导出漏洞 |
| F2 diff 身份 | 仍存在→本轮已修复。真实 ChangeTracker 中 model.pyi/model.py 前缀与 hunk 正文 b/z.py 导致错误归属 | `_diff_by_path` 从 header 建立精确新旧路径映射，五个消费者共用；删除单文件任意回退。普通 unified、Markdown 包装、Git、重命名/增删/带空格引号和非 ASCII 路径均覆盖，歧义省略 |
| F1 裁决权限 | 仍存在→本轮已修复。allow_refund 合法需求在真正语义请求前直接 revise；仅改为 allow_pattern 就改变入口 | 风险进入 `_evidence` 的非权威假设；删除提前硬裁决；`_merge_compatibility_risk` 只补充真实 revise 的诊断，不覆盖 accept/blocked/trace。真实 Runner、Python 测试和完成门验证 accept、revise、无效结果 |
| F3 词法身份 | 仍存在→本轮已修复。完整 analyzer→index→collector 的参数/赋值/别名遮蔽仍导出错误类 | 已有 AST 记录根绑定，calls/refs 同步持久化、解析；参数、局部赋值（含调用后赋值）、局部导入、嵌套定义、闭包/条件/global 等保守处理；已知遮蔽不落回唯一符号或 LSP 邻近类；动态接收者保留候选，精确定义可增强非类目标，不确认遮蔽参数的类身份。保留可确认导入、局部定义、实例方法；覆盖增量、warm reload与迁移 |
| F4 内容同源 | 仍存在→本轮已修复。完整采集在同大小/mtime 内容变化后仍输出旧边+新源码 | `PersistentCodeIndex._parse` 用同句柄读取的原始字节哈希绑定解析；constructor/dependency collector 同时校验调用者与目标；不一致整体省略并记录 fallback；正常增量刷新恢复。schema6通过既有重建替换旧缓存，不给旧 AST 补今天的哈希 |

F3 与 F4 共用 schema6，因此合为一个依赖完整的小提交，没有建立新解析器、版本服务或验证器。无收益的风险否决已删除；真实测试、权限、版本与完成要求继续沿原链路执行。

## 生产链证据

[authorized-gate-replay.json](authorized-gate-replay.json) 保存真实 NativeSDKRunner/AgentRunner、ToolExecutor、文件工具、VerificationManager、Python 测试子进程、保留需求、主/辅助受控请求与状态。Provider 仅返回协议内容，不替 Agent 写文件或执行测试；实际测试默认拒绝负数、显式允许退款和正数行为均通过，真实语义审查后 completed。名称不同的等价 gate 入口相同。受控 revise 和 invalid 的 [边界](review-revise-boundary.json)、[边界](review-invalid-boundary.json) 仍是 max_turns，兼容性要求保持 candidate，未写入语义通过证据。单元回归另覆盖 accept/revise/blocked 及解析异常；既有失败、拒绝与陈旧写入回归保留。

[repository-packets.json](repository-packets.json) 使用真实文件、ChangeTracker、RepoIntelligenceService、持久索引和 SidecarVerifierHook._evidence/build_verifier_user_message，未手工组造正确证据。四个实际包依次为：

1. 无遮蔽，`return Alpha()` 提供 Alpha 的完整类源码。
2. 参数 Alpha 遮蔽，调用身份 unresolved，不再提供导入类。
3. 外部将 Alpha 改为等长 Bravo 并恢复 mtime，未刷新索引时省略构造器源码，审查缓存身份改变。
4. 既有增量刷新后，仅提供 Bravo 类源码。

相关回归还覆盖目标类同元数据改变、定义范围失效、UTF-8/CRLF原始字节、纯 touch、解析后改变及屏障控制的组包竞争。内容哈希在 AST 同一次读取就绑定；摘要变化本身不能替代同源证明。采集仅访问有界相关文件，没有每轮全仓哈希或同步重索引。

每个文件的源码捕获按路径去重一次；为检出组包期间变化，末尾另做一次有界字节复核，每阶段最多每文件一次。这是两阶段一致性检查，不能同时声称只读取一次或具有操作系统级原子快照。可用性/内容改变会改变 supporting digest，纯 touch 和无关索引 generation 不改变接受缓存身份。

## 命令与结果

失败日志、修复后的定向组合结果见 [regressions.txt](regressions.txt)。展示副本仅去除行尾空白，原始捕获摘要及持久位置见 [log-provenance.json](log-provenance.json)；受控请求和 packet JSON 原样保留。执行过的主要命令：

```text
python -m pytest -q tests/evaluation/test_agent_core_diagnostic_v1.py
python -m pytest -q tests/test_package_structure.py tests/runtime/test_task_reference_evidence.py tests/test_terminal_infcode_commands.py
python -m pytest -q tests/test_terminal_infcode_commands.py tests/runtime/test_task_reference_evidence.py
python -m pytest -q tests/test_sidecar_verifier.py tests/runtime/test_review_evidence_identity.py tests/runtime/test_requirement_scope_runtime.py
python -m pytest -q tests/test_code_index.py tests/test_call_usage_role.py tests/test_python_lexical_bindings.py tests/runtime/test_constructor_evidence.py tests/runtime/test_semantic_dependency_evidence.py
python -m pytest -q tests/test_python_lexical_bindings.py tests/test_package_structure.py tests/test_call_usage_role.py tests/runtime/test_constructor_evidence.py tests/runtime/test_semantic_dependency_evidence.py
python -m pytest -q
python -m compileall -q nz_coder
python -m nz_coder --help
ruff check .
git diff --check
```

本地完整运行仅一次，提交 `31aaad9` 下为 **5 failed, 4248 passed, 36 skipped in 745.85s**，见 [完整输出](full-suite.txt) 和 [状态](full-suite-status.json)。未超时，也不宣称本地整仓全绿。五项不是历史 CI 的同一组五项：

| 当前失败 | 分类、处理与最终复核 |
|---|---|
| `test_paid_money_reference_does_not_block_completion` | 本轮临时测试环境问题：PYTEST_PLUGINS=nz_review_progress 与独立验收重设 PYTHONPATH 冲突，明确 ImportError。去除临时插件后，运行同一生产链/真实独立验收通过；没有改产品或该测试 |
| `test_unresolved_call_retains_raw_target_and_candidates` | 本轮回归：清空动态接收者候选过宽。保留未确认候选但不确认目标，原断言未变 |
| `test_bounded_lsp_augmentation_upgrades_unresolved_call` | 本轮回归：全挡动态参数增强过宽。恢复精确非类定义增强，同时追加遮蔽模块成员不能认证成类、参数位置不能映射邻近类的反例 |
| `test_reexport_and_unresolved_dynamic_reference_contract` | 同一候选信息回归，保留候选恢复；未改变原断言 |
| `test_index_rebuild_and_transport` | 本轮遗漏同步 schema 固定号，5→6；source_start_line、迁移清库、冷/热重建和传输断言保持 |

针对这五项及相关链路的 [修复后组合](post-full-regressions.txt) **247 passed in 31.24s**：

```text
env -u PYTEST_PLUGINS PYTHONPATH=/tmp/nz-review-offline-guard:/home/pyh/nzcoder python -m pytest -q tests/test_repo_identity_v3.py tests/test_repo_intelligence_closure.py tests/test_source_start_line.py tests/test_python_lexical_bindings.py tests/test_code_index.py tests/test_call_usage_role.py tests/runtime/test_constructor_evidence.py tests/runtime/test_semantic_dependency_evidence.py tests/runtime/test_requirement_scope_runtime.py
```

此前的旧 compaction 两例 `test_context_overflow_stops_after_three_compaction_attempts`、`test_pre_send_and_reactive_compactions_share_one_three_attempt_owner` 在此次完整运行均通过，不是本轮残留失败，也未修改断言。`test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget` 和 `test_subagent_stops_after_configured_turn_budget` 触发90秒栈记录后都通过；后者主线程在 RepoIntelligenceService.close 的 executor.shutdown/join。仅登记耗时及栈，未展开新专项或断言是死锁。

36 skipped：35 项原生 Windows 句柄/junction/reparse/process-job/smoke 在 Linux 不适用，1 项固定 InfCodeX 受控双侧 smoke 为既有 opt-in 未启用；逐项名称与原因保留在完整输出，没有新增 skip。历史 CI 37 skipped 不能直接套用到当前环境。

最终代码静态检查 `python -m compileall -q nz_coder`、`ruff check .`、`git diff --check` 均 exit=0；CLI help smoke exit=0。远端 CI 尚待推送后核对，不能用定向通过替代最终 SHA 的 CI。

完整测试设1200秒总上限；90秒无返回记录线程栈与当前用例，超时先取栈再终止。测试使用私有 HOME 和环境白名单，Python 出站限定回环/Unix 本地契约；主/辅助模型边界均受控，不继承真实密钥或代理。完整运行若失败，保留原始结果，修复后只重跑受影响集合，不通过重复整仓测试碰运气。

实际模型/embedding/线上评测请求 **0**。离线受控请求与 usage 不作为线上效率或成功率证据。历史付费材料未改写，未启动新对测。当前本地 Linux、Python3.13，Windows 与其他 Python 版本的本机实跑不在覆盖范围；远端 CI 按最终 SHA 单独报告。

所有模型边界仍是离线受控，新增回归不证明真实模型语义裁决能力。源码包、Python3.10/3.12与安装 wheel 的远端结果以最终提交 Core Runtime 为准；本地仅执行上列平台/版本。此阶段到七项根因和新增回归闭合为止。
