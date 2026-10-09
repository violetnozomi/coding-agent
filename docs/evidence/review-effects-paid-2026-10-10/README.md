# G/D 真实审查与 A 自主执行

本轮已执行一次固定实验，随后关闭在线授权。**新版 G 获得有效 accept；两版 D 均指出默认负数拒绝缺失；旧版 G 被原生规则误拒，没有调用审查模型。A 在第 8 个主请求的真实测试后，经真实审查接受，走既有 early completion 正常结束，冻结终态独立验收 9/9 通过。**这些是已知开发／回归样本的单次观察，不表示总体成功率或 InfCodeX 对齐比例。

用户授权经验计数及 G/D＋一次 A，随后指定“10元人民币／美元，授权到本轮结束”。宿主按较低的 **10 CNY** 执行，直接复用项目环境中的 DeepSeek 账号，不公开密钥。授权包括主请求、审查、所有辅助和失败尝试；未借用历史预算。请求/token/超时提案在本轮授权下生效；未用完的额度不用于重跑或校准。

输入是官方参考编码的**估算准入／预留**，没有可证明的服务端上界。原严格模式仍拒绝；原 `technical_ready=false` 保留，授权经验模式另行准入。账号硬消费限额未验证；用户已接受在途、计数和计费偏差风险。详情见 [执行补充](execution-supplement.json)、[本次使用的授权](authorization-used.json)及[准入状态](online-admission.json)。历史清单未覆盖或改写。

| 固定身份／实际配置 | 本次事实 |
|---|---|
| 实验适配基线 | `9bbf320072123d0e2a69a3e21a2a6eb8fd07895d`；开始时工作树干净，无后续增量 |
| 实际适配提交 | `cc7a43c53499cc4212cc100d06606119615aab81` |
| 旧／新 Core | `cf2ff5cf078559e9843c34614318d80c984b4168` / `4abbcafae69f2e3b9d162ad73e769913659aca59` |
| 冻结材料 | `91bf79826c39733e606f3f694ebbc7970eba1356`；manifest SHA `ce3533973916c837fdc7e6c9827af9eb3d793e6d5c76684dbb942a8d6612e994` |
| InfCodeX | 历史 `d3a812379b589597347f5be12d5b68477e577f02`；本轮 **not_run** |
| 请求／返回模型 | `deepseek-v4-flash` / `deepseek-flash`；返回 fingerprint `aeb56401ca74e127821c4f9126dcb669` |
| 主请求 | thinking/effort **未设置**；stream=true，max_tokens=64000 |
| 4 次实际审查请求 | thinking=disabled，effort未设置；stream=false，max_tokens=1024 |
| 环境 | 复用冻结旧／新运行包、镜像及 38 个锁定 wheels；容器 Python 3.12.13；没有重建／下载依赖 |
| 隔离 | 每单元独立工作区/HOME/缓存；只读运行包；Docker network=none；宿主仅转发到固定 DeepSeek endpoint |

官方当前说明：旧 Flash 别名由 V4.1-Flash 服务，按 Flash 价格计费；主请求继续保留冻结别名，不重命名历史 B/N。此事实来自[官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，此次直连读取回执与原页哈希见 [pricing-receipt.json](pricing-receipt.json)，没有额外身份探测请求。

| 顺序 | 来源、实际请求 | 裁决与应用 | 冻结独立验收 |
|---|---|---|---|
| u01 G／新 | 真实模型，1 | raw accept，`verifier_ok`；Hook complete，写入语义完成事实 | 三项行为均通过 |
| u02 D／旧 | 真实模型，1 | raw revise，`verifier_ok`；指出忽略 allow_refund 导致默认接受负数；Hook reanimate | 仅默认拒绝失败 |
| u03 G／旧 | 原生规则，0 | `deterministic_compatibility_guard` 把用户明确允许的 opt-in 当成兼容破坏；Hook reanimate | 三项行为均通过 |
| u04 D／新 | 真实模型，1 | raw revise，`verifier_ok`；指出同一默认拒绝缺陷；Hook reanimate | 仅默认拒绝失败 |

各单元的完整理由、有效性、trace、前后账本和真实后果见 [results.json](results.json) 及 `u*/result/review.json`。准备阶段每单元 3 个受控协议响应，只让原生 Runner/read/edit/bash 构造冻结候选和运行真实测试；不提供审查 verdict，不计远程主模型调用。每单元第一次裁决后即停止，D 没有再付费修复。两版实际进入审查的都是运行时生成的提前结束文本，冻结 neutral report 没有被主流程消费；没有强制消除原生 early completion。

D 的行为缺陷检出正确，但理由不能整体当成正确事实：旧 D 说测试只覆盖正值，新 D 说现有测试仍期望“一律拒绝”；实际可见测试同时覆盖显式负数 opt-in 和正值。两份理由还批评最终说明缺少局限。这些附带理由单列，不能当成额外缺陷检出或测试事实。

首个有效行为分歧是 **G：旧版前置规则误拒，新版真实审查接受**；没有绕过 u03 来凑第四次请求，也没有声称是旧／新两个模型判断的直接对照。本次涉及多项既有修复，不能把结果单独归因到某个字段。

A 的完整要求未缩减，默认 policy 未覆盖，Provider 没有预置补丁或按请求编号安排工具。真实主模型选择了如下步骤；完整参数、请求、响应和工具事实见 [requests.json](requests.json)、[时间线](a01/timeline.json)和 [原生 runtime](a01/result/runtime.jsonl)。

| 主请求 | 实际动作 | 版本／验证事实 |
|---|---|---|
| 1 | list_directory | 初态 |
| 2 | 读取 6 个核心文件 | 明确 API、Event、传播、输出和 CLI |
| 3 | 读取测试、README、包入口 | 核对旧行为及交付要求 |
| 4–5 | 查看 adapters 并读取 3 文件 | 跨层传播定位；未改代码 |
| 6 | 4 个 apply_patch | generation 0→4；API/Event/service/formatting |
| 7 | 原生 write_file 修改 CLI、两份测试、README | generation 4→8；保留旧断言并增加 None、空串、JSON/文本等覆盖 |
| 8 | 实际 Bash `python -m pytest -q tests` | 14 passed，verification_generation=8，后续没有写入 |
| 辅助审查 1 | 真实 `emit_sidecar_verdict` accept | `verifier_ok`；指出 None/空串、传播、CLI/测试/文档证据 |
| 原生结束边界 | finalize/completed | `semantic_review_and_ledger_satisfied`，semantic_review_generation=8，unresolved_requirements=[] |

8 次主响应均为 tool_calls；**没有主模型无工具终答**，首次主模型结束尝试保持 null。运行时在已通过当前版本验证、真实审查接受后合法提前结束，未预填“下一轮 completed”。没有强制首次失败测试，也没有可独立验收的完整中间文件快照；只能确认已记录的修改、当前版本测试及冻结终态，不能追认更早版本已全部正确。

冻结后，隔离的评估者对副本运行同一项目测试并执行原冻结的 9 组外部断言；原终态哈希未变。**项目复测 14 passed，功能、兼容、None/空串、CLI、README 九项 9/9 通过**；见 [验收](a01/independent-acceptance.json)、[最终 diff](a01/final.patch)和 [最终文件](a01/final)。外部验收没有反馈给模型，不修改代码，也不替代 Agent 内真实执行的证明。

总共 **12 个物理请求：8 主＋4 审查**；其中 G/D 3 审查，A 8 主＋1 审查。摘要、stall、embedding、重试均为 0；原生 u03 没有虚构 usage。所有 12 次参考输入与报告 prompt 相等，delta=0；无超预留、无非法／冲突 usage。有限样本匹配仍只支持经验口径，不晋升为严格计数。

| Provider 原始计量 | 数值 |
|---|---:|
| prompt / completion / total | 102054 / 4472 / 106526 |
| prompt 缓存命中／未命中（已包含在 prompt） | 56704 / 45350 |
| reasoning（已包含在 completion） | 578 |
| 按官方峰值价的保守费用估算 | **0.12874416 CNY** |
| 空闲价估算 | 0.06437208 CNY |
| 真实账单／账户硬限额 | unknown／未验证 |

宿主按峰值输入 2、缓存输入 0.04、输出 8 元／百万 token 预留，不因缓存预期或空闲价格降低准入预留。以上是价格×usage 的估算，实际扣款可能受账户赠送额等影响，不能当成账单。NZ runtime 的 output 不含 reasoning，而 Provider completion 含 reasoning；以本次原始 Provider usage 汇总，不把两者、缓存和 SSE 片段重复相加。原生 cost=0 配合 unknown_calls 也不能解释为免费。

本轮只改 evaluation relay、启动器和对应本地边界夹具，没有修改生产 Core。strict 拒绝未知上界；empirical 必须有明确风险／费用授权并绑定模型、范围、最终 payload 哈希及代码身份。未知计量或超预留会停止整个实验；原件保存目录/文件为 0700/0600，响应捕获有 32 MiB 上限。105 项相关回归通过，包含本地假上游下的真实旧／新 Runner/Judge 链路；独立增量审查未发现重要问题。命令见 [commands.md](commands.md)，结果见 [test-results.txt](test-results.txt)。没有再次运行完整 Core 套件、Windows 或历史 compaction 专项，也不沿用过期固定失败清单。

唯一后续候选是**审查投影丢失相关已读测试正文**：u04 的 VerifierContext 含实际测试 read，但最终 Provider 请求只列 read_file 调用，缺少测试原文。固定新版 `_render_transcript`（`sidecar_verifier.py:275`）只呈现 user/assistant，不呈现 tool 结果。见 [投影核查](coverage-projection-check.json)。这能证明信息缺口；不能证明它导致附带理由错误，D 的拒绝仍正确。下一轮可先用这份固定 packet 和请求做离线反例，核对既有投影是否能在有界预算内保留相关测试事实；**本轮不实施，不新建 verifier，不新增硬裁决，也不付费消融**。

原始抓包仍在受控本机目录，本仓库仅提供脱敏派生数据。[raw-index.json](raw-index.json)逐件区分原件与公开投影的 SHA-256；协议原请求中的私有推理字段未因公开日志处理而删改。公开材料移除推理正文、完整环境及宿主私人路径，保留数字 usage 和合法审查理由。完整结果及轨迹已冻结，在线授权关闭；不启动 InfCodeX、B/N、校准或更多任务。
