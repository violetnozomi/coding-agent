# 100题真实Trace归因与一次事务版本修复

原100题仍是 **80通过、19未通过、1验收阻断**。本轮只修复“同一事务的自身编辑被误判为外部版本变化”；离线生产链路已证明缺陷修复，**没有运行新题的付费A/B，也没有证明自主编码成功率提高**。

审查A：`99eee2967ba8ff4dbfb17bf241e382b8fa022f5a`，开始时HEAD与origin/main一致、工作树干净。修复B：`5e4f5f0cdd9451a612cc337f75d9d7eb4b936cd4`。没有回退、覆盖用户修改或使用旧费用余额；`.env`未改动、未归档。

## 1. 原100题实际证明什么

四批20/20/40/20题均有真实主模型请求，使用deepseek-flash与官方Pro V2冻结终态验收。80份最终补丁通过相应官方断言，证明这一配置下存在跨Python、Go和JavaScript/TypeScript等项目的实际定位、修改、复测能力；不证明所有中间工具成功、履行全部Agent内验证义务或所有自然语言需求完整。80/19/1是已有样本结果，不作为本轮优化收益。

实际主请求3387、辅助出站0；3394条llm_request含本地检查，不冒充实际请求。Agent终态为39 completed、51 max_turns、8 error、2缺少result，不能用完成状态替代官方评分。[逐题表](task-statistics.md)、[全部统计字段](task-statistics.json)保留两套结果和缺失项。

入口是NativeSDKRunner的 **MAIN_PROFILE / declared-agent-graph** 单coding-task声明图，不是普通终端native-coding-loop，也不是managed多Agent。100题没有planning_start、planning_done或task_contract_bootstrapped；98份可用状态未生成plan、任务合同、需求ledger或精确验证contract，2份缺失记unknown。RunSettings规划/reflection默认关闭。[原生产入口](../swebench-pro-20-batch4-2026-10-12/native-entry.py)、[原有效配置](../swebench-pro-20-batch4-2026-10-12/live-execution-config.json)可追溯，不能把未启用的规划、合同、Reviewer收益归因到成绩。

已有工具事务/CAS、STALE_READ、编辑恢复、检索、Artifact、VerificationManager、工作量提示确实运行。269条verification_result、116条verification_result_ignored、65条strict_progress_nudge、214条work_budget_pressure是实际事件。L1/L2事件各3条不等于模型评估成功：辅助边界存在本地拒绝，实际付费辅助请求为0。

## 2. Bash为什么大量未执行

1851次Bash调用中941次进入处理函数、910次未进入。`executed=true`仅代表handler进入：无效timeout在启动进程前返回，不能写成已运行。runtime把错误简化为“Tool execution failed.”，下一主请求的ToolResult却包含具体错误；这不是证据捕获丢失。统计读取的是实际模型请求，不只看runtime摘要。

|具体原因|调用/涉及任务|判定依据与处理|
|---|---|---|
|评测授权回调拒绝首个shell单词|599 / 92|四批permission语法树相同。cd、环境变量前缀、gofmt不在回调白名单；“Denied by user”不是599次人工拒绝|
|回调私有路径/隐藏遍历限制|183 / 74|匹配真实permissions allowed=false，再核对strict_private_tool_input_violation；不取消保护|
|工作区外路径|122 / 69|实际返回path outside workspace，与权限回调拒绝分开|
|其他工具权限策略|6 / 5|原ToolResult保留，不归并为前缀问题|
|进入handler后：宽范围测试限制|45 / 37|调度失败，测试未启动|
|进入handler后：timeout参数非法|25 / 22|要求180/300/600秒但已有上限120；未启动，不是测试失败|
|真实120秒超时|2 / 2|与无效参数区分，不算通过|

[工具事实与真实请求交付位置](tool-evidence.json)包含call_id、原runtime行号、触发请求和模型实际收到结果的请求。599个回调前缀拒绝属于实验条件；绕过它会改变权限口径，所以没有选择为Core修复。具体频次、其他候选和竞争解释见[阶段一归因](phase1-findings.md)。

## 3. 七个重点任务

[七题逐个主请求表](focus-timelines.md)链接到每次真实请求、响应和工具事件。关键结论如下。

|任务|事实与归因|没有据此得出的结论|
|---|---|---|
|b4-15|#6的useLink.ts全文550行实际送到#7，#7读测试，#18/19取得错误码；之后仍调查，40请求0编辑|不是读正文丢失；不能仅据此认定某个Core门应强迫提前修改|
|b4-09|#4/6/11/12已读目标、hook、测试和调用方；#35才编辑，#36误STALE并回滚，#40仅形成schema/Metric补丁|晚调查、机制选择函数/hook/meta.size遗漏早于此bug；修复不意味着此题可通过|
|b4-16|42 Bash中20次未进入handler；真实Go报缺cue模块、GOPROXY=off；最后仅探针，未形成validate/schema功能|未执行的系统路径探测不证明缓存不存在；不是增加轮数就一定解决|
|b4-13|业务修改到#39；三个Redis文件进入正式patch，官方应用退出97，未跑测试|初态只有tracked摘要，没有证据确定Redis文件原有或运行生成；不清洗原补丁后补记通过|
|b4-01|#32/33才修改，两次Go资源失败；#39非法timeout未启动；#40仅复合build EXIT=0，最后tool_calls/max_turns|冻结验收通过不能倒推Agent已完成可靠目标验证；没有证明“过度验证”或完成门bug|
|b4-02|#11修改，#15直接pytest17 passed，#17ruff、#18静态检查，#21 stop；generation=3验证关联，正常结束且官方通过|两次辅助审查本地拒绝，不宣称Reviewer模型接受|
|b4-19|#7 newosproc，#12自增测试的断言失败，#14修改该测试预期；#15/19/26目标输出ok，#30 stop，官方通过|不是每次断言失败都证明实现错误；pipe tail的可靠性限制未放宽，generation仍-1|

19个未通过任务中11个没有成功文件写工具结果；这是候选策略问题，不能用预设补丁的离线回放证明提示能改善自主行为。至少24题可见摘要有fork/exec资源不足，全部最终通过；环境失败和功能失败必须分别判断。

## 4. 为什么只修事务版本

选择标准依次为真实失败可定位、同条件可复现、是否能窄化修复及风险；不是选择最高频日志。5题出现7次同文件同批STALE_READ，均有同请求中的前序成功写入：[逐事件证据](selected-root-events.json)。4题后来绕开并通过；b4-09发生在末段。

生产错误边界：[ToolExecutor.check_write_freshness](../../../nz_coder/runtime/execution/tool_executor.py)每次写入比较模型已读V1；文件工具已经写入V2，但[ToolRuntime](../../../nz_coder/runtime/tool_runtime/pipeline.py)只在整个事务提交后更新读取基线。于是后续精确编辑把自己的V2当外部变化，失败后整批回滚。写调用是串行的；没有外部夹具的最小任务同样失败，排除了“外部写入/并发写调度才是原因”。

另一个必须保留的竞争解释：**b4-09录制第三个锚点本身不匹配**。修复后第二项可执行，第三项返回真实EDIT_NOT_FOUND和候选行，整批仍应回滚。该问题与需求遗漏没有被本轮修好。不能用最终原补丁失败反证这个局部修复无效，也不能宣称本轮修好了b4-09。

## 5. 唯一生产修改与红绿行为

复用[MutationReceipt/TransactionManager](../../../nz_coder/state/transaction.py)，由[WorkspaceFileAccess](../../../nz_coder/foundation/workspace_file_access.py)在同句柄完成写入并发布后记录对象身份和内容摘要。只有当前活动事务的已落地票据与本次受控读取的对象、内容都一致，才把变化视为自身编辑；不把任意当前磁盘内容升级成模型已读，不删除STALE_READ，不改变权限或CAS。

同一票据也约束既有rollback：外部原子替换或原地修改后保留外部内容，沿现有rollback_partial报告冲突，而不是覆盖它。这个配套反例在旧版也复现；它是同一事务所有权修复的安全约束，没有新建版本服务。dry_run、失败、取消及事务退出后的票据不能提供成功提交依据。

[源码差异](core-change.patch)仅3份生产文件；[真实运行时回归](../../../tests/runtime/test_transaction_owned_edit_runtime.py)复用已有Node运行夹具，只替换模型边界，实际运行NativeSDKRunner、AgentRunner、ToolExecutor、投影、VerificationManager和测试子进程。

|相同动作：read → 同批3个精确edit → Node测试|A原版|B修复版|
|---|---|---|
|实际主/辅助受控请求|3 / 0|3 / 0|
|第一个编辑|执行|执行|
|后续编辑|误STALE_READ，未执行|执行并提交|
|事务|回滚，恢复初态|成功提交|
|Agent内真实Node测试|失败|通过，当前generation关联|
|终态|保持max_turns|既有完成边界正常completed|
|冻结文件独立Node复测|exit=1|exit=0|

初态SHA两侧同为`30df3ed3bb78f7ebca3c5a5faeb3f9e22bae1144bf8ee21a9e98ebd474aab704`；B终态为`1ed1118f2c6e396781befa35841f5b560e7587af05676b1bbe5ccb7c2e679418`。原始受控请求、工具事实、状态、权限、diff、独立stdout保留于[离线A](offline-replay/A/result.json)与[离线B](offline-replay/B/result.json)。usage为替身测试值，不是线上效率或费用。

改动前的正确化夹具已暴露两次误STALE，原始前置诊断保留于[pre-fix diagnostic](offline-replay/pre-fix-diagnostic.json)；隔离git archive A运行相同回归也RED。初次夹具错误曾把tuple helper拆错而触发Provider断言，该试验已排除，不能充作RED。

## 6. 实际验证与限制

完整命令、失败断言和跳过理由见[验证记录](checks/README.md)。新增11个行为回归通过；相关组合曾44 passed/5 skipped，另一个不重叠核心/工具/安全/Skill组合518 passed/6 skipped。覆盖合法同批编辑、真实坏锚点及回滚、录制片段、提交后连续编辑、外部原子/原地变化、own delete/create、dry_run、取消、权限拒绝和真实复测失败。没有把脚本化正确动作说成模型学会修复。

两个历史compaction fake在当前基线A与修复B **各2 passed**；旧报告中的失败本轮未复现，未修改测试或预算代码，不把通过归因于本轮事务改动。`ruff check .`仍有基线已存在的test_model_relay.py:233 E731；本轮新增和修改文件的ruff通过，未顺手修无关计费测试。

只运行Linux / Python 3.13.12，Node `/usr/bin/node` v18.19.1；11项跳过均为未运行的Windows句柄/junction语义。没有运行Windows或全仓pytest；也不证明任意外部并发编辑的所有竞争窗口安全。历史证据原件1094份SHA复核不变，原100题结果及补丁不修改。

## 7. 未见任务效果证据仍缺什么

[8题预冻结设计](ab8-design.md)排除原100题，以固定种子排序后选取，没有看评分换题；16份公开instruction/task.toml核验官方SHA，8份Linux镜像manifest固定digest，仅查询清单、不拉镜像。A/B381份Core文件身份固定，差异仅本次3份生产文件。[完整manifest](ab8-experiment-manifest.json)逐题记录两侧not-run、验收null。

未来每题A一次+B一次，固定模型、权限、镜像、初态、40主/6辅助请求、时限与评分器，保存所有结果，不挑更好的补丁混算。预算总额与相等每次准入待**新授权后、首个请求前**冻结；旧余额不延续，本轮主/辅助/embedding收费请求均0。

仍缺独立新题上的真实模型对照，包括机制是否被触发、误拦截是否减少、功能验收是否改善以及费用影响。8题甚至可能均不触发该机制；那只能报告未观察到收益，不能提高轮数、换题或重跑到成功。这是研究消融，不是leaderboard pass@1。

[三个面试复盘案例](interview-cases.md)均引用真实事件。本轮止于这一有证据的修复与离线验证，不展开其他Core模块。
