# Linux coding baseline V1 — P2 固定十题批次

## 本轮交付：A，离线接线完成，尚未获得 P2 付费授权

2026-09-09 从 `7c4835afc85b566651af638643501bfdae15ccfb` 续接。
P1 已结束；本轮没有重跑 T01/T04，没有运行付费健康探针。
P2 真实任务启动 **0**，真实模型新增调用 **0**，新增费用及未知预留均 **0 CNY**。
计划 10 题、启动 0、端到端成功 0、独立补丁验收通过 0、已测失败 0、未运行 10；
真实成功率和补丁通过率为 **null**，不是 0% 或离线通过率。

既有 P1 授权不覆盖 P2。本轮仅提交可复用接线、未授权示例和报告，未创建新的
`authorized=true` 配置，也未创建真实批次分配登记或正式实验输出目录。

## 固定计划与最小接线

唯一 P2 顺序为 **T02 → T03 → T05 → T06 → T07 → T08 → T09 → T10 → T11 → T12**，
串行、每题最多一个正式 attempt。空计划、重复、未知任务、T01/T04、任意子集和改序均拒绝。
P1 双题和单 T04 兼容入口保留，但没有 `stage=P2`、两份前序引用及新的明确授权，不能进入 P2。

| 任务 | 固定测量内容 | 本轮真实状态 |
| --- | --- | --- |
| T02 | Decimal 精确求和与迭代器 | not_run |
| T03 | 词频相同时的确定性排序 | not_run |
| T05 | lower 参数贯穿 library 与 CLI | not_run |
| T06 | include_archived 参数贯穿 library 与 CLI | not_run |
| T07 | Unicode slugify 小功能 | not_run |
| T08 | project 汇总及 CLI 输出 | not_run |
| T09 | 按指定列顺序投影映射行 | not_run |
| T10 | 空输入 average_hours 边界 | not_run |
| T11 | 保存文件前完成输入验证 | not_run |
| T12 | 批量追加的输入验证原子性 | not_run |

修改范围限于 `evaluation/linux_baseline/` 和相关测试。没有新增逐题续接模块、Agent 循环、
调度数据库或 Provider 计费平台；`nz_coder/`、catalog/manifest、原验收和参考修复保持不变。

- `live.execution_plan` / `authorized_policy`：唯一固定计划、P2 授权结构、批次和单题金额上限。
- `continuation.carryover`：复核 T01 与 T04 的封存证据，重建费用，保留未知预留。
  原 T04 登记只读。P2 共用一个私有登记 `p2-batches/<原实验ID>/remaining-ten`，
  绑定完整 grant hash、前序证据、任务计划和输出目录；换实验名不能再次分配。
  复用排他 mkdir、落盘同步和 ready 标记，失败后不自动释放登记。
- `live.worker_request` / `serial_remainder`：组织器和 worker 共用描述符及整个已运行前缀检查。
  每个前序任务按当时余额重建 journal，并核对 worker 摘要、result、组织器摘要、启动标记和描述符。
  当前任务的 Session、需求、任务树哈希、授权集合、版本及余额在客户端创建前核对。
- `billing.claim_attempt`：按验证过的计划排他创建任务目录，默认 P1 范围不扩大。
  原 HTTP 请求预留、输出限制、SDK 重试、主/辅助共账本、未知用量停止规则未改。
- `live.run` / CLI：普通功能失败保留并继续；配置、账务、验收基础设施或清理不可靠时停批次。
  P2 单题时间/轮数/Token 限制终止也保守停批次，不沿用 P1 允许 `budget_exceeded` 前缀的行为。
  任务准备失败留存部分目录与零启动结果；账务前缀后来损坏时，批次及原预算余额均标为 null。
  只有全部选中任务 success 才退出 0；失败或未运行退出非零，仍保留完整汇总。

冻结对账及下一请求预留均发生在下一任务 materialize/worker 之前。
正式批次不是可恢复任务队列：重复输出目录、已登记批次、已启动/不确定 attempt 不自动重试。

## 版本、配置与免费核验

候选 P2 harness 是本报告与接线所在的提交；交付消息记录精确 SHA。
正式 `freeze` 要求该 SHA 与干净 HEAD 一致。授权前的只读草案不是已授权的真实实验身份；
获得确认后才固定授权引用、新 experiment_id、唯一输出目录和最终 config hash。

Agent revision 沿用 `7c308e3a75deae112e20c0de225113fda6ec9f9e`。
manifest SHA-256 仍为 `e6dbaf4407e96ec4c814951028d265588f027d796fe313b57adcae88a247ae89`。
freeze 保存 Agent tree、系统指令来源 blob、各任务初始树/验收哈希、依赖、完整受限工具表和前序证据。

| 参数 | 拟沿用的固定值 |
| --- | --- |
| 入口 | 正式 Linux headless CLI → Native |
| 账户/endpoint/model | 原官方 DeepSeek；`https://api.deepseek.com`；`deepseek-v4-flash` |
| 主请求 | thinking enabled / high / 最大输出 8000 |
| 辅助验证 | thinking disabled / 无 effort / 最大输出 1024，保留强制判定工具 |
| 单请求输入预留上界 | 1,048,576 Token，不假定缓存命中 |
| 单题累计用量预算 | 2,000,000 Token，涵盖受控主/辅助请求；不是上下文窗口 |
| 单题执行上限 | 主循环 30 轮；整个 worker 墙钟 600 秒，含辅助调用 |
| 工具/功能 | T04 原 16 项受限白名单；planning/reflection/MCP/子 Agent/付费 embeddings 关闭 |
| 重试 | SDK 上限 2；每次重试重新经过 transport；首个不确定结果后禁止再次发送 |

凭据只从此前允许的项目 dotenv 必要连接字段解析，结果为**凭据已解析、Provider/model 匹配**。
未输出 key、完整环境或配置，也未查询账户。解析成功不等于此次已验证远端认证/服务可用。
组织器实际启动时仍需使用 workspace 外的 HOME；不继承开发者历史记忆或其他任务 Session。

复用 `p1-env`：Python 3.13.12、pytest 8.4.2、openai 2.36.0；pytest 在项目要求 `>=7,<9` 内。
没有重建环境或重复 P0 十二题准备/参考解题流程。

### 费率依据和请求准入

沿用 [官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) 的既有封存证据：
查阅时间 `2026-09-08T06:47:24Z`，文件 SHA-256
`899affbdbc33d0be620d8dea59e86f5036c11b5410b14d060b8d2874c74f38e5`。
本轮重新核对了本地证据，不声称 09-09 重新在线查询或看到过账户账单。
该日期是查阅日期，非费率生效日期；记录的版本标识为 DeepSeek-V4-Flash-0731。

单位是 **CNY / 百万 Token**：缓存命中 0.10、未命中 3、输出 9，
沿用跨时段的官方峰值上限折算；未把空闲时段的半价当作全时段保证。
输入输出共享 1M 上下文；预留完整输入上界后再加输出上限，是有意保守超额预留，
不是宣称模型能同时接收 1M 输入再额外输出。

主请求预留 `(1,048,576 × 3 + 8,000 × 9) / 1,000,000` = **3.217728 CNY**，
Token 预留 **1,056,576**；辅助上限请求预留 **3.154944 CNY / 1,049,600 Token**。
建议的批次 6.219802 CNY、单题 5 CNY、累计 200 万 Token 能准入首请求。
每题可用金额是单题上限与当前整批余额的较小者，不提前为十题各锁五元。

可靠 usage 结算后释放差额；超时、失联、取消或 usage 不完整不会视为免费。
预留周转量不是实际花费；usage 折算不是独立核实的账户账单。
余额非零仍可能低于下一请求预留：**6.219802 CNY 不保证跑完十题**。
不足时停止并保留余题 not_run，不降低预留或增加授权金额。

## 历史预算对账与未授权范围

对账读取两份原授权/冻结/结果/账本，并校验 T04 原排他登记；不是将提示词中的数字直接填入账本。

| 层次 | 已知 usage 折算费用 | 未解决预留 | 剩余账本额度 |
| --- | ---: | ---: | ---: |
| T01 原实验 | 0.2115294 CNY | 3.154944 CNY | 当时 6.6335266 CNY |
| T04 续接 | 0.4137246 CNY | 0 CNY | 当时单题额度剩余 4.5862754 CNY |
| 两个历史实验合计 | 0.625254 CNY | 3.154944 CNY | **6.219802 CNY** |
| P2 本轮 | 0 CNY（未请求） | 0 CNY | 尚未正式分配 |

拟申请 P2 整批最多 **6.219802 CNY**、单题最多 **5 CNY**，全部来自原余额，不追加预算。
约束为 `0.625254 + 3.154944 + P2 已知费用 + P2 未解决预留 ≤ 10`。
T04 未用额度已经包含在 6.219802 中，不重复相加；其辅助费用也已包含在 0.4137246 中。
旧请求14仍 unknown，既不释放，也不认定实际已扣款。所有余额均不是独立核实的账户余额。

未发现活动旧 baseline runner/worker，也未发现两个历史实验之外的后续运行支出记录。
两个原实验目录和 T04 登记保持只读；本轮未清理任何历史现场。

## 离线验证及修复证据

首次相关 P1 基线检查：**169 passed**，退出 0。
新增测试先在旧接线上出现预期失败：P2 计划/CLI/help 3 处、P2 历史承接 2 处、
真实组合链路被 `claim_attempt` 的 P1 白名单拒绝；均不是导入或依赖失败。
进一步测试复现了初始树篡改未拒绝、任务准备错误没有最终报告、
前序账单损坏仍发布旧余额、资源耗尽后继续执行等边界，并已最小修复。

最终实际执行：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_live.py tests/evaluation/test_p1_billing.py \
  tests/evaluation/test_p1_sidecar.py tests/evaluation/test_p1_continuation.py \
  tests/evaluation/test_p2_batch.py tests/test_headless_cli.py --tb=short
```

结果：**231 passed in 186.47s，退出 0**。
其中 P2 新增 62 项，包含六种真实组织器 → worker → headless Native → 受控 transport 组合分支。
无真实授权文件、真实 key 或网络请求进入测试；临时脚本响应和补丁不计入成绩。

三题组合测试送入 worker 的批次金额依次为 **6.219802、5.197802、4.175802 CNY**；
第三题结束剩 **3.153074 CNY**，不足主请求预留，所以没有创建第四题目录或启动 worker。
这是刻意设置合成 sidecar 用量得到的离线金额，不是真实费用。
独立功能失败组合保留第一题失败并继续；usage 缺失、摘要丢失/不一致、验收基础设施错误均停在第二题。
主请求仍 enabled/high/8000，真实 sidecar 调用链仍 disabled/无 effort/1024/强制工具。
正常响应得到 `accept/verifier_ok`；缺 usage 分支得到 `accept/provider_error`，并保留未知预留、停止发送。

另已执行并通过（均退出 0）：

```bash
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner --help
.nz-coder-runs/p1-env/bin/python -m ruff check \
  evaluation/linux_baseline/{billing,continuation,live,live_worker,runner}.py \
  tests/evaluation/{test_p2_batch,p1_controlled_worker,p2_controlled_responses}.py
.nz-coder-runs/p1-env/bin/python -m compileall -q \
  evaluation/linux_baseline/{billing,continuation,live,live_worker,runner}.py \
  tests/evaluation/{test_p2_batch,p1_controlled_worker,p2_controlled_responses}.py
git diff --check
```

只读独立复核发现的旧余额误报已用回归测试修复。没有修改 `nz_coder/` 产品源码，故没有扩展全平台回归。

## 后续启动与分析边界

[未授权 P2 示例](../../evaluation/linux_baseline/p2-unauthorized.example.json) 是结构示例，
金额是待确认建议，`authorized=false`、授权引用为空；不能直接用于 live。
本轮未执行真实命令，因此不提供伪造的“已执行 P2 live 命令”。
确认后只使用现有 `runner --live --live-config ... --output ...`，填写实际路径，
不附加 `--dry-run`/`--publish`，不提前创建要求独占创建的正式实验目录。
运行期间版本、费率、任务/验收和功能配置不变；结束后才追加实跑结果。

本轮缺的是一次明确 P2 授权，不是 T04 授权，也不是新的架构开发。
确认范围：固定十题各一次，最多使用原余额 6.219802 CNY，单题 5 CNY，
保留旧未知预留；采用本报告主/辅助模式与停止规则，不重跑 T01/T04、不追加预算或自动重试。

尚无 P2 真实失败轨迹，因此暂不归因于模型、检索或完成门控，也不凭离线脚本提出 Agent 改造。
本轮下一步仅是取得授权后收集固定版本真实结果；届时再以具体任务证据选出一个优先改进方向。
T01/T04 历史结果单独归属此前各自 harness，不能拼为同一版本下的 12 题成绩。
整个任务集仅为两个本地 fixture，不代表 SWE-bench、复杂开源项目、Windows HTTP 或完整多 Agent 产品验证。
