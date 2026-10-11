# 固定 20 题正式执行与闭账结果

本轮固定的 SWE-bench Pro V2 公开集 20 题均完成一次真实 NZ-Coder Agent 尝试；没有按结果换题，也没有付费重跑失败题。原生 NativeSDKRunner、AgentRunner 和文件/测试工具实际执行，供应商为本轮授权的 DeepSeek `deepseek-flash`。这不是受控 Provider 回放。

**官方独立验收：17 题通过、3 题未通过、0 题未知。Agent 终态：9 completed、9 max_turns、1 error、1 missing_result。** 8 题在 max_turns 后冻结补丁仍通过官方验收；保留原终态，不事后改成 completed。官方通过仅证明对应验收范围，不能追认 Agent 阶段未执行的测试或全部自然语言要求。

固定官方源码 `scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862`；名单 SHA-256 `1432a813774c92c0817a24a81abdd952f0fae7b4dc482760f5116b395d1f797c`。起始 NZ HEAD `98171657a6d2be083fed2d39c74f80138ddb98e5`。每题环境、实际 native entry 哈希和启动命令在对应目录；环境兼容与输出限制续接有后续增量，不能将整个批次称为完全同配置对测。本轮没有 InfCodeX 新请求。

## 逐题记录

| 题号 / 复盘入口 | 实际主请求 | Agent 终态 | 官方独立验收 |
|---|---:|---|---|
| [01 RHEL 衍生发行版版本号归一化](attempts/01/README.md) | 16 | completed | 通过 |
| [02 表单解析与嵌套默认值](attempts/02/learning.md) | 40 | completed | 通过 |
| [03 封面 URL 白名单与导入副作用](attempts/03/learning.md) | 36 | completed | 通过 |
| [04 PlayIterator 状态枚举与调用方迁移](continuation-attempts/04/README.md) | 40 | max_turns | 通过 |
| [05 过期时间与发送调度不能共用最低时间规则](rest-attempts/05/learning.md) | 29 | completed | 通过 |
| [06 计量止损与未完成任务](attempts/06/learning.md) | 11 | error | 未通过 |
| [07 Windows KB 数据与预期同步](continuation-attempts/07/learning.md) | 27 | completed | 通过 |
| [08 搜索耗时、Agent 超时与验收失败分开复盘](pending-attempts/08/learning.md) | 17 | missing_result | 未通过 |
| [09 UUID 分配的幂等性](continuation-attempts/09/learning.md) | 35 | completed | 通过 |
| [10 字典文本的安全解析](continuation-attempts/10/learning.md) | 37 | completed | 通过 |
| [11 邮件引用区间不能吞掉后续内容](rest-attempts/11/learning.md) | 40 | max_turns | 通过 |
| [12 GUI 子进程 stderr 实时呈现](continuation-attempts/12/learning.md) | 40 | max_turns | 通过 |
| [13 监控调度器从阻塞运行迁移到 asyncio](rest-attempts/13/learning.md) | 39 | completed | 通过 |
| [14 状态栏自定义文本与预定义组件混用](rest-attempts/14/learning.md) | 40 | max_turns | 通过 |
| [15 没有配置文件时也必须应用环境变量](rest-attempts/15/learning.md) | 40 | max_turns | 通过 |
| [16 图片代理的辅助函数与实际加载链路必须同时接通](rest-attempts/16/learning.md) | 40 | max_turns | 未通过 |
| [17 上传前验证目标目录，避免隐式创建](rest-attempts/17/learning.md) | 40 | max_turns | 通过 |
| [18 新增电子书提供方需要覆盖检索和阅读入口](rest-attempts/18/learning.md) | 40 | max_turns | 通过 |
| [19 把自定义 home 贯通启动配置、profile 和密钥目录](rest-attempts/19/learning.md) | 40 | max_turns | 通过 |
| [20 区分非 ISBN 的 ASIN，并保留来源优先级](rest-attempts/20/learning.md) | 15 | completed | 通过 |

每题目录保留脱敏请求/响应、工具和权限事实、初终态文件、冻结 patch/哈希、官方独立验收日志以及复盘。完整原始响应（包括未公开 reasoning）与 Session/Artifact 校验归档保留在 `/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11`。公开 raw-identity 索引可核对原件，缺失或半截响应不补造。02/03 的原评分环境问题与修正后的官方执行都保留；03 的额外扩大范围检查有失败，不能称全项目全绿。

## 三个未通过样本

- **06：** 原阶段 Provider 报告输出 8001 token，超过当时人为 8000 预留而止损；空补丁独立验收失败。后续按用户要求移除主请求的人为输出上限，但没有重跑本题。
- **08：** 大范围搜索耗时、最后请求超时且缺失 usage，外层停止后缺少正常 SDK 最终导出；恢复原始 Session 日志，未虚构 run_end。冻结空补丁重新独立验收未通过；不重新运行 Agent。
- **16：** URL helper 单项通过，实际消息组件的代理加载与失败后直连仍失败。新增 action/reducer 未完成接入，且 Agent 未执行功能测试；冻结结果独立验收失败。

## 模型请求和预算

- 真实出站 **662 主请求、0 辅助请求**；19 次本地辅助准入拒绝另记，不能算收费模型调用。661 请求有有效 usage，08 的 1 请求未知；所有请求 ID 唯一，有结算记录，无遗留在途预留。
- 已知 usage 的峰时保守估算 **35.72062032 元**；未知请求继续全额保留 **3.190104 元**，预算占用 **38.91072432 元**，小于授权 50 元。未使用授权 **11.08927568 元**不沿用到下一轮。供应商实际账单未取得，这些数值不是实际扣费。
- 已知 token 为 prompt 18,381,850、completion 925,224、total 19,307,074；因 1 请求未知，全批 token 总量仍未知。这些是 661 请求的小计。cached 输入与 reasoning 不重复相加。
- 实际出站 model 均为 deepseek-flash；**stream、thinking、reasoning_effort 字段均未设置**。不能把 SDK 使用非流式结果收集写成 wire 明确传了 stream=false，也不能把未设置的 effort 写成 medium。
- 前 103 请求有显式输出限制；用户要求移除后，后 559 请求没有 max_tokens/max_completion_tokens。393216 只用于费用预留，不是给模型新增的请求限制。保留每题原配置，不混称同一固定输出预算。

费用交接、四份账本哈希、逐题原件校验及出站配置计数见 [final-accounting-and-integrity.json](final-accounting-and-integrity.json)。本轮结束，协调器、存储守卫和发布观察进程均已退出，不继续调用模型。

## 执行、隔离与验证边界

实际运行依次使用 `python docs/evidence/swebench-pro-20-2026-10-11/run-pro.py --execute`、`continue-unattempted.py --execute`、`continue-unattempted.py --preparation-retry --execute`、`continue-unattempted.py --after-task-timeout --execute`；准备重启只针对零次模型调用的题，原请求和账本不重置。题序中的缓存环境排序不依据成绩，全部固定 ID 最终均已尝试。

Agent 在隔离 HOME、无网络、非 root 的容器中使用项目工具；主机密钥仅在既有 relay，所有模型边界计入同一授权。Ansible/旧项目使用已授权的隔离 Agent Python，项目与官方验收解释器保留。官方脚本在补丁冻结后、全新原始镜像上运行，未进入 Agent 或提示词。没有主机代写 patch、预填答案或人工纠偏；环境准备、依赖下载、归档恢复和评分环境修正分别留痕。

05/11/16 的原 512 MiB 归档配额失败保留，完整源证据未删；扩大到有磁盘核验的 2 GiB 配额后生成校验归档，再回收本轮临时源目录。原错误不改写为编码失败或成功。20 份归档与冻结 patch 的 SHA-256 均核对；662 对原始请求/响应哈希、20 个实际结果及 788 份公开文件的密钥检查通过，原始 reasoning 未公开。

相关既有离线续接回归为 16 passed；它只证明续接与费用保留语义，不代替本次真实模型任务。没有重跑全仓或 Windows，也没有新增 Core 循环、verifier 或预算框架。两项历史 compaction fake 失败仍单列在 [N 结束边界审计](../n-terminal-audit-2026-09-19/README.md)，未弱化断言或重开专项。

后续值得核对的单项候选是多题中笼统的 `Tool execution failed.`：16/17/18 多个调用未执行，19 实际 Go 编译遇到 OS 线程限制后，部分调整命令也未执行。20 改用 workdir 后目标 pytest 真正通过。这里只登记执行事实，尚未据此确认统一 Core 根因或放宽权限。

这是固定小样本、每题一次的真实任务记录，不输出总体 SWE-bench 成功率、对齐百分比或模型能力提升结论。
