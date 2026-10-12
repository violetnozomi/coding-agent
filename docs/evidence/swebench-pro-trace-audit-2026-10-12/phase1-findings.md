# 第一阶段：先归因，不修改Core

基线99eee2967ba8ff4dbfb17bf241e382b8fa022f5a；开始HEAD和origin/main一致，工作树干净。本阶段已读取四批100个实际任务的原始模型工具选择、实际请求中的ToolResult、权限、runtime、RuntimeState和冻结补丁。原80通过/19未通过/1验收阻断不改写。逐题数字见task-statistics.json/.md；工具事件原文件行号、call_id与实际模型接收请求序号见tool-evidence.json，原件SHA见historical-evidence-sha256.json。没有付费调用。初步归因与无外部修改的RED在Core修改前完成；本文件中第三锚点及外部回滚反例的进一步结果是后续核验追加，不冒称当时已经知道全部原因。

## 主要候选与唯一选择

|分析优先级/候选|频次及结果影响|可复现/修复风险|本轮决定|
|---|---|---|---|
|1 同批自身局部写入被误当外部变化|5任务7次STALE_READ，同主请求、同文件、前序成功写入；4题随后通过，b4-09末段回滚|明确生产边界、离线必现；窄化但须守住外部变化和rollback，安全反例已测|**唯一Core修改**|
|2 模型调查不收敛/需求遗漏|19未通过中11题没有成功文件工具结果；b4-15全文和测试可用仍搜索|结果影响大但尚无单点Core证因；提示效果只能新模型实验验证，误催编辑风险高|保持待验证，不强迫编辑/扩建planner|
|3 评测回调拒绝shell前缀|599调用、92任务；cd、环境变量前缀、gofmt；模型看到Denied by user|回调必现但属实验权限口径，改动会影响安全/对照条件|记录，不作为Core安全缺陷、不放宽|
|4 Go资源耗尽与并行编译|可见摘要至少24任务有fork/exec资源不足，均最终通过；b4-01/19含newosproc|环境证据强，但并行度、冷缓存为竞争解释；改变并行资源会改变比较条件|登记环境因素，不追加专项|
|5 验收补丁混入运行时文件|b4-13三个Redis文件；官方应用退出97，一题验收阻断|补丁冲突必现，来源证据不足；清洗补算会改变patch身份|原件/unknown保持，不补算通过|
|6 私有目录/隐藏遍历保护|183调用、74任务；ls -a、grep -r、私有路径等|多数保护有意生效，频次不证明缺陷；取消保护风险大|记录，不取消|

此顺序是综合频次、结果影响、证因强度及修改风险的本轮处理优先级，不是按日志次数给Core缺陷排名。高频但属于实验条件或有意保护的条目不优先改Core；未完成任务数也不等于某个策略造成失败的数量。

高频的评测拒绝不能靠修改Core偷偷绕过；调查策略缺少可证明的单点实现错误。选择同批STALE_READ，是因为存在真实生产失败和窄化的确定性反例，且影响一个未通过任务末段的拒绝原因与恢复证据。不能据此说它解释了b4-09的全部遗漏，或修好后该题必然通过。

## 七题因果归纳

|案例|真实链路|排除/边界|
|---|---|---|
|b4-15|#5定位useLinkInner；#6取得useLink.ts全部550行（27375字符，fetchLink出现7次），#7读测试。#8–40继续查错误码、缓存和backoff，0次文件修改；#18/19已取得RESPONSE_CODE线索|全文确实进入#7实际请求，不是正文丢失。6次Bash未处理：1越界、4回调私有/隐藏限制、1前缀。缺少修改是假设/策略停滞，不能直接给一个工具bug定罪|
|b4-09|#4读fileSaver、#6读hook、#11读测试、#12读数据传递调用方；#35才写schema；#36同文件三局部编辑：第一项成功，后二项STALE_READ，全批rollback；录制回放确认第三锚点原本不匹配，即使去除误拦截，这批仍必须rollback；#40改用atomic apply_patch，最终只补schema/Metric两文件|机制选择函数、hook、meta.size传播仍未实现；公开工具结果和最终diff可核实。晚调查与遗漏先于本轮bug，不能全部归因到回滚|
|b4-16|42次Bash，20在处理前被拦截；#31后只写_probe和internal/cue/probe.go。真实编译报告no required module provides cuelang.org/go/cue及GOPROXY=off；未形成validate或嵌入schema|网络与依赖环境可见，未执行的外部路径探测不证明缓存不存在。探针能编译也不等于完成功能|
|b4-13|#39才改业务文件；最终patch把appendonlydir三个文件作为new file；原freeze使用git add -A，初态仅git ls-files；官方应用发现目标已存在，测试未启动|runtime工具记录不含对这三个文件的文件工具写入；无法确定是原镜像未跟踪文件还是测试运行产生。没有补造初态归因|
|b4-01|#32/33才修改keystore；两次runtime-owned Go compile真实资源失败；#39timeout=参数无效未启动；#40复合go build输出EXIT=0，但没有可靠当前版本目标行为验证；最后仍tool_calls|冻结验收通过不能倒推Agent已具备验证/完成契约。空ledger、contract_passed=true是未设契约的空成立；max_turns不单独证明过度验证|
|b4-02|#11起实际修改，#15直接pytest17passed、#17ruff通过、#18静态验证；#21自然stop/完成；当前mutation_generation=verification_generation=3|两次辅助审查在本地拒绝，不能说语义Reviewer已接受；独立官方验收通过与正常结束均有证据|
|b4-19|#4起写入；#7真实Go因newosproc失败；#12限制并行后的自增测试出现真实断言expected1/actual2；#14修改的是该测试的预期；#15/#19/#26输出目标测试ok；#30自然结束，官方独立通过|不把断言失败自动归因于实现缺陷，也不把修改测试预期算作再次修好生产代码。目标测试输出和OS失败分开。通过调用用了pipe tail；VerificationManager拒绝以末段退出码记通过是现有正确保守行为，verification_generation=-1，不放宽它|

## declared-agent-graph的实际范围

NativeSDK入口明确构建一个coding-task声明图，RunRequest为MAIN_PROFILE；运行事件control_plane=declared-agent-graph。并非直接终端的native-coding-loop入口，也不是managed multi-agent评测。100题没有planning_start/planning_done/task_contract_bootstrapped事件；98份可用RuntimeState均未生成plan/任务合同/需求ledger/精确verification_contract，另2份状态缺失保留unknown。RunSettings规划和reflection默认关闭，不将未运行能力归因到成绩。

共用的ToolRuntime、文件/CAS、编辑恢复、VerificationManager、仓库检索、工具Artifact、工作量提醒确有运行事件；Bash处理事实与模型收到的信息分别审计。L1/L2孤立事件不证明辅助请求真实出站，按账本和Provider原件另计。空合同并不证明所有自然语言需求满足；正常结束也不等于官方功能验收通过。

## 第二阶段前的设计与竞争解释

当前ToolExecutor按模型已读版本校验每次写入；file tool完成一次原子写入后，仅在整个ToolRuntime事务提交时更新已读基线。因此同一事务内下一次精确修改看见自己的新内容，却仍与事务前的观测比较。回放同时确认第三锚点错误；该竞争解释保留，不能承诺原录制批会全部提交。scheduler已串行化写入，这不是写调用并发导致。

最小修复仅让现有活动事务的MutationReceipt携带成功写入内容的摘要；下一次写入的受控读取只有同时匹配该事务已落地对象身份和内容摘要，才能视为自身进展。不是先把当前磁盘任意版本认作已读，也不是关闭STALE_READ。未完成/失败、dry_run、其他执行上下文、事务退出后的票据都不能提供此依据；继续使用现有CAS与rollback。

另一个在旧版复现的配套反例表明，原rollback会覆盖外部替换的目标。使用同一落地版本校验约束回滚，发生身份或内容冲突就保留外部内容并沿既有rollback_partial报告。

先以未修改Core运行真实Runner的read→同批多次edit反例，再实施。无外部进程、同文件唯一锚点和串行工具时仍必须复现；加入外部原子替换、拒绝、失败、取消/rollback及后续批次的反例。预期变化是合法同批多编辑提交，不再误STALE和rollback；外部版本变化仍拒绝，测试失败仍不能被记为通过。
