# 已读测试正文的审查投影修复

起始 HEAD：`24a4002909d0e979586e12e964f5da6f97d53c28`，main，工作树干净，无后续增量。实现终态提交：`dd0316df9578ec2736e1eda9feb8c2c9c2641b72`。本目录随后追加证据；最终提交与同步状态以交付回复及 `git log` 为准。本轮远程模型请求 **0**；上一轮费用授权未复用。未复跑 G/D、A、B/N 或 InfCodeX。

根因成立于信息投影，不等同于模型错误理由的唯一原因。原 u04 成功读取的测试正文在 Runtime Tool Part/配对结果中，但 `_render_transcript` 只展示调用，审查请求没有正文。本轮不把它归为 InfCodeX 的同名缺失机制，不给对齐比例。

| 链路 | 原行为 | 当前行为 / 证据 |
|---|---|---|
| `read_file` → Tool Part/结果 | 已有同句柄身份、正文和展示范围 | 不修改；最小脱敏夹具保留真实调用、成功结果、身份和实际测试事实 |
| `build_verifier_context` → 请求 | 24 条滚动调用描述，无工具正文 | 裁剪前从当前任务最多 96 条已有记录选择；相关已读测试独立引用 |
| `_evidence` → 文件时效 | 原无该正文区段 | 既有 `WorkspaceFileAccess` 只验证身份；正文始终来自已交付的结果，不读盘补正文 |
| `_accepted_cache_key` | 未计入测试正文 | 加入实际展示正文、范围、状态及选中文件的既有身份摘要；不哈希整个会话 |
| 执行与结束 | 既有验证、需求账本和完成门 | 不改；读取不写 passed、不刷新 verification_generation、不替代执行 |

生产改动只有 [sidecar_verifier.py](../../../nz_coder/runtime/verification/sidecar_verifier.py) 的 Context、构建、展示、缓存和 Hook 接入，以及小型纯投影模块 [read_test_evidence.py](../../../nz_coder/runtime/verification/read_test_evidence.py)。未新增审查器、证据存储、读取服务、Agent 循环或 mandatory review。

## 历史请求与新的离线投影

原件仍在 [付费 u04 请求](../review-effects-paid-2026-10-10/u04/provider/da927cc71d684ef3aa3439a62c30880d.request.json)。文件 SHA-256：`3a396110b67fd2ce744077d1ad0afde3edf49e326944fc23e96dd2356f37f39b`；已逐字节对照起始提交，未改变。测试观察摘要：`110e59b6207fcee91510d47dfd1efc472dd03ca3bdea3005333a39ada464a855`。

[历史实际消息](historical-u04-user-message.txt) → [新的离线消息](new-offline-u04-user-message.txt)，[增量 diff](projection.diff) 与 [结构化对照](projection-summary.json)可复查。新的消息**未发给真实模型**，不是改写历史请求。

新增块 578 字符，含标题、路径、1–3 行、原文件内容摘要、来源、完整性和版本状态；整条 user message 从 4465 变为 5045 字符。除新增块及分隔换行，原任务、diff、执行事实、依赖/构造器证据和最终文本逐字一致。系统提示仅加两句来源与缺失证据说明。

u04 投影包含准确的两个断言，并保留 `from payment import amount`。没有隐藏验收、预期 verdict 或 G/D 标签。由于这里只能核对历史观察，版本标为 `historical_unknown`，不冒称目前宿主磁盘内容。正文恢复不证明真实 D 的附带理由已经纠正。

## 身份、预算、缓存和权限

- 按 call_id 配对实际 `read_file` 调用、completed Tool Part 与结果，再核对消息、run、session、workspace 和路径归属。Part 与 tool 消息只展示一次；调用未完成、拒绝/取消/失败、孤立结果和 user 伪造 metadata 不产生正文。
- 明确验证范围优先，其后是任务点名或已修改测试；无关普通源码不抢预算。短完整文件优先。同文件同版本重叠重读保留更完整连续范围；新版本成功读取替代旧版。
- 最多 3 文件，每文件 JSON 记录最多 2000 字符，总块最多 6000 字符，包含标题、状态、路径与省略说明。正文来自真实展示范围，不使用 preview，不挑孤立 assert。截断后 `complete_file=false`，无 EOF 标志，范围/半行状态明确。字符限制不是精确 token 保证；原最终请求预算检查和输出限制未改变。
- 文件时效不看全局 generation。最多 3 次受控身份核对，每次最大 1 MiB；超限/不可访问只能标历史不可核实。核对 hash 与设备/文件身份，忽略仅 touch。测试被改、删或替换不能把旧正文标当前；当前 diff 仍是另一个来源。
- 缓存绑定实际纳入的正文、展示范围、版本可用性与设备/文件身份，不绑定调用 ID、mtime、回答措辞或无关读取。缺正文→可用、变更/删除/替换会失效；等价重读、unchanged 缓存 stub 和 touch 保持稳定。
- 新区段用 JSON 引用源码，标明低信任、非指令、非执行事实；伪造标题/指令留在字符串中。未增加 Sidecar 工具或权限，仍只有 `emit_sidecar_verdict`。这不是“提示注入已彻底解决”的结论。

## 真实离线运行链

[运行时回归](../../../tests/runtime/test_read_test_review_runtime.py)使用 NativeSDKRunner → AgentRunner → ToolExecutor → 真实 read_file / pytest / edit_file → VerificationManager → 原 Sidecar Hook → Provider 边界截获并 JSON 序列化。所有主/辅助模型解析都替换为离线 Provider，并禁止 socket 出站；没有只替换主模型后留下收费辅助边界。

[链路与主请求实际消息](runtime/chain.json)、[实际审查边界序列化请求](runtime/serialized-review-request.json)、[文件 diff](runtime/workspace.diff)及[冻结文件复测](runtime/frozen-retest.json)保存如下事实：

1. 真读取 payment.py 和 6 行测试正文，观察在 generation=0。
2. 真 `python -m pytest -q tests`：1 failed、2 passed；失败送达下一主请求。
3. 真局部编辑只加入 `and not allow_refund`；generation=1，测试本身未变。
4. 同命令真复测：3 passed；既有契约/verification_generation 关联当前版本。
5. 审查请求含完整 fixture/三个断言、路径、1–6 行、内容摘要及 `version=current`；受控 accept 后原完成门在工具边界结束。4 主请求、1 受控辅助请求，无预设下一条“结束”响应被调用。
6. 冻结文件再次独立执行 pytest，3 passed、exit=0；这不反向证明每个历史工具均成功。

[裁决应用记录](runtime/verdict-applications.json)：accept → completed；revise/invalid → max_turns，各 5 主请求、2 受控辅助请求。它们只证明裁决应用和完成控制流，不证明模型自主判断能力。另有真实测试仍失败的运行时反例：读取正文没有变成 passed，也没有完成。

## 验证结果与限制

命令全文在 [commands.txt](commands.txt)。测试日志展示副本仅清除行尾空格，原始输出仍在命令记录所列 `/tmp` 日志；历史付费原件和请求未处理。基线隔离副本复用相同最小夹具，`test_captured_read_source_reaches_final_review_message` 原实现失败于正文断言；当前通过，[基线失败全文](baseline-red.txt)。新增 40 项行为回归覆盖配对/批次、任务与运行隔离、版本/touch/删除/替换、范围/预览/截断、预算、缓存、注入和完成门。非 Python `checks/amount.cjs` 确实经过文件读取工具；这个新案例不声称执行了 Node 测试。

| 实际检查 | 结果 |
|---|---|
| 新回归 + Sidecar + 原审查身份 | [92 passed](focused-results.txt) |
| 参考证据、实际调用方、Node 验证、原生 Runner、投影、读取、恢复、Skill、已有预算 | [389 passed](caller-results.txt) |
| 独立文件安全/事务组 | [60 passed、3 skipped](security-results.txt)，跳过均为 Linux 未运行的 Windows junction 语义 |
| 首次大组合 | [536 passed、3 skipped、2 failed](initial-combination-results.txt)，不能写成全通过 |
| 相同既有用例的原基线组合 | [500 passed、3 skipped、1 failed](baseline-combination-results.txt) |
| ruff / compileall / git diff --check | 通过 |

组合失败是 `test_directory_limit_stops_enumeration_early`（17 != 3）与 `test_directory_listing_does_not_materialize_unbounded_entries`（69 != 3）。二者独立进程均通过；第一项在原基线组合也失败。共同 helper 全局替换 `os.scandir`，会累计其他枚举；存在并发干扰嫌疑，但本轮没有捕获首次失败的线程归属。**第二项不能直接归为已证明的历史失败。**额外有观测的较小组合 53 passed 未复现；未改断言或生产目录逻辑，以原日志登记，未扩大修复。

此前登记的两个 compaction fake 失败 `test_context_overflow_stops_after_three_compaction_attempts` 与 `test_pre_send_and_reactive_compactions_share_one_three_attempt_owner` 本轮未复测，也未修改/弱化，不据此宣称已关闭。未运行 Windows、完整全仓套件或付费模型。

已证明：相关已读测试正文不再在审查投影阶段丢失，身份、范围、预算、缓存和完成语义经过离线回归。未证明：真实 D 理由纠正、总体审查准确率、编码成功率或量化 InfCodeX 差距。本轮停止于此。
