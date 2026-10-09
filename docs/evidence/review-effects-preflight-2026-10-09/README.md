# 审查效果验证：离线准备完成，真实效果未运行

本轮固定比较旧 NZ `cf2ff5cf078559e9843c34614318d80c984b4168` 与新 NZ `4abbcafae69f2e3b9d162ad73e769913659aca59`。开始时 HEAD 就是新版，工作树干净，无后续增量。生产代码没有改动，两版本使用各自未修改的 detached worktree。InfCodeX `d3a812379b589597347f5be12d5b68477e577f02` 仅为历史参考，没有启动参考 CLI。

**四次真实审查和一次自主任务均为 `not_run: missing_paid_authorization`。** 本轮附件明确规定旧预算、密钥或余额不能作为新授权；当前没有覆盖本轮模型、账号、金额及全部辅助调用的授权。没有读取密钥或发送模型/embedding 请求，费用为 unknown/null，不写成零价格。

## 冻结输入与独立验收

[manifest.json](manifest.json) 在抓包前冻结，[freeze.json](freeze.json) 保存其摘要。执行顺序固定为 G/新 → D/旧 → G/旧 → D/新，每个真实决策计划一次；自主任务另计。模型、token、请求和期限是待授权提案，不是已获批准的配额。主请求上限12、辅助6、四次审查最多8个物理请求，总计最多26；失败与重试同样计数。宿主转发层的 token/请求硬限额尚未接入核验，因此也不宣称在线启动就绪。

退款样本复用上一阶段已知机制，不能称未见泛化。任务要求显式 `allow_refund=True` 放行负数，默认仍拒绝负数，非负输入保持原行为。两个候选来自同一初态；任务、可见测试、依赖和说明完全相同。候选目录使用中性编号，无评分标签或预期裁决。G/D映射只在评分材料中。

| 冻结候选 | 实现差异 | 本轮真实独立验收 |
|---|---|---|
| [candidate-01](prepared/gate/candidate-01/payment.py) / G | 原负数守卫增加显式参数条件 | 三组检查全部通过 |
| [candidate-02](prepared/gate/candidate-02/payment.py) / D | 删除负数拒绝守卫 | 显式放行、非负行为通过；默认负数拒绝失败 |

可见测试只查显式放行 `-3` 和默认正数 `3`，未覆盖默认负数拒绝，两侧各自真实执行后均为2 passed。隐藏检查另测默认负数、显式负数和非负行为；[验收记录](gate-independent-acceptance.json)包含对应失败事实。有限数值样本不是全输入形式化证明。隐藏检查没有进入工具工作区、主请求或审查包，外部验收不追认历史工具成功。

## 实际生产边界观察

[packet-capture.py](packet-capture.py)复用各版本自己的既有 `_run` 设施：NativeSDKRunner → AgentRunner → ToolExecutor → read/edit/Bash → 真实 pytest → VerificationManager → StopHook → `_evidence`/`build_verifier_context`/`build_verifier_user_message` → 原生 judge/Gateway 边界。未手填 ledger、generation、semantic_pending 或 passed；没有把新版包送进旧版 Hook。

离线协议响应只构造冻结实验输入，不评价主模型自主能力。保留原生早结束检查，因此三个准备动作后就抵达审查；预备的中性终答没有被消费。实际包中的结束文字是 Runner 生成的当前版本精确测试摘要，两版本一致。这些包包含各自真实 diff、任务、测试事实和完成要求；索引各自预热，旧 schema5、新 schema6，数据库互不共享，正常批准缓存没有关闭。

| 单元 / 包 | 离线生产路径观察 | 真实审查结果 |
|---|---|---|
| G/新 [u01](packets/u01.json) | 风险作为非权威假设进入审查包，抵达请求边界 | not_run |
| D/旧 [u02](packets/u02.json) | 没有该守卫风险，抵达请求边界 | not_run |
| G/旧 [u03](packets/u03.json) | 模型前 `deterministic_compatibility_guard` 返回 revise/reanimate；未调用审查 Provider | not_run；离线规则拒绝单列 |
| D/新 [u04](packets/u04.json) | 抵达请求边界 | not_run |

旧版对G的实际理由是：`allow_refund` 不是来自新输入语法，应改成 input-specific gate。这与明确允许参数开启行为的任务冲突，独立验收提供反证。新版没有执行这项前置否决，风险文字已进入后续审查边界；**真实模型是否接受G、是否准确指出D，仍未知**。不能用D碰巧被别的理由拒绝计作检出。

其余三个单元在离线 Provider 边界主动拒绝发送，没有返回 verdict。生产回退事件出现 `accept + trace=provider_error`，不构成有效批准；兼容性 ledger 仍为 candidate，没有语义通过记录。抓包后即停止离线准备，没有让回退字符串成为最终任务完成证据。[review-results.json](review-results.json)把真实结果和离线观察分开，真实 raw/effective verdict、理由成立与运行时后果均为 null。

## 自主任务与有效参数

选既有 A_unknown_location 跨文件关联ID任务，与退款任务及正确补丁分开。这是已知回归任务，不称未见任务。只复制 [初态](prepared/autonomous/initial/README.md)和[原任务](prepared/autonomous/task.txt)，不复制 oracle。初态真实公开测试3 passed，独立验收1/9；见 [autonomous-initial-checks.json](autonomous-initial-checks.json)。公开测试绿不等于新增API、传播、CLI、文档要求完成。

[autonomous-dry.py](autonomous-dry.py)通过实际 NativeSDKRunner 构建首个出站请求，真实 OpenAI-compatible 序列化后由本地 MockTransport持有，不返回动作或补丁。原生工具保持注册，未设置检索策略或偷传policy metadata；实际 `repo_retrieval_decision.strategy=policy`。请求中的会话、记忆、配置来自私有空HOME，环境为白名单。

| 参数 | 实际离线序列化事实 |
|---|---|
| 主模型 | deepseek-v4-flash；stream=true；max_tokens=64000 |
| 主 thinking / reasoning_effort | 字段未设置，不推断服务默认 |
| 审查 | stream=false；max_tokens=1024；thinking.type=disabled；effort未设置 |
| 主逻辑轮次配置 | RuntimeState.max_turns=12；物理预算仍须宿主转发层落实 |
| 实际传输 | 本地MockTransport，未连接服务；历史 api.deepseek.com 只作为待授权配置 |

[干跑请求](autonomous-dry-request.json)与[摘要](dry-run-provenance.json)保留真实生效字段。审查参数探针不含候选包，不是一条审查裁决。干跑故意持有传输后产生的 error/provider_turn 只属于干跑，不算真实主请求或自主任务失败。[autonomous-result.json](autonomous-result.json)的功能、验证、交付、终止和关键时点均为未运行/null，没有用预设动作补成自主结果。

## 隔离、准备问题与验证范围

所有离线进程使用既有 `offline_exec.py`，内核拒绝IPv4/IPv6，子进程继承；三个审查边界未发送，主/审查/摘要/stall/embedding均不能付费出站。普通文件工具实际拒绝工作区外的输入、代码证据目录和父目录路径；声明pytest的Bash回调只允许该本地命令。

**文件工具限制不是宿主文件系统隔离。** 尝试现有 bwrap 时，网络命名空间配置及uid map均被环境拒绝。离线准备只执行受信任的固定输入，没有自主模型；尚不能证明将来工具子进程无法访问评分者父目录。在线阶段除费用授权外，还需满足这个隔离条件；不会用网络隔离替代文件隔离证明。未启动宿主在线转发层。

离线适配中出现的三项问题已登记，没有修改Core：

- u01首次准备把 `../workspace` 错当越界，实际解析回当前工作区，断言失败。改为真实越界 `../input.json` 后只重建该失败准备；[初始失败](initial-preparation-failure.json)与本机初始工作区保留。其他三个包没有重跑。
- 抓包脚本初始 source 映射用了内部初值 `deterministic_guard`，实际生产trace是 `deterministic_compatibility_guard`。公开派生观察按结构化trace和 `provider_invoked=false` 更正，原始本机捕获未改写；u03保留 normalization 来源。
- 自主入口干跑先误读不存在的 `env.max_turns`，再误以为边界异常一定向外抛出。修正为请求配置和真实RuntimeState，并在finally采集实际Runner返回。两次失败仅为准备适配问题；第二次已序列化但未发送请求，不当作自主运行。没有在失败后改变模型、effort或轮数。

本轮只运行真实候选/初态测试、独立验收、抓包、请求干跑与只读证据检查，没有重跑整仓测试，没有新增skip。使用Linux、Python3.13；未运行Windows、TUI、参考CLI、其他生产入口或真实模型。上一阶段最终CI为4254 passed/37 skipped；两个旧compaction例在该版本已通过，本轮未重跑，不沿用“当前有两个固定失败”。

[commands.md](commands.md)记录执行与继续边界；[verify.py](verify.py)核验冻结哈希、逐单元模型状态、证据、版本、脱敏和请求字段；[SHA256SUMS.json](SHA256SUMS.json)覆盖交付文件。完整本机原始捕获留在受控artifact目录，公开包仅替换路径前缀，不含密钥、完整环境或reasoning_content。

本轮证据检查与脚本ruff检查均exit=0。本机原件定位为 `~/.codex/artifacts/nz-review-effects-2026-10-09/runs/u01..u04/raw/capture.json` 与 `autonomous/dry-request.json`；[preparation-receipt.json](preparation-receipt.json)记录两份代码树身份、准备计数及未运行范围。原始SHA与公开派生SHA分别保存，不将脱敏副本说成逐字原件。

## 当前能回答什么

1. 新版是否避免旧规则误拒：离线确认移除了该样本的前置否决；真实语义结果未知。
2. 真实缺陷是否仍被发现：未知；D的独立反例已确认，没有真实裁决。
3. 新证据是否抵达审查边界：离线生产边界已确认；真实服务未收到。
4. 真实理由是否正确使用证据：未知，没有真实返回。
5. 自主任务是否满足功能、验证、交付并正常结束：未运行，不能判定。
6. 审查是有效纠偏还是无依据返工：旧规则在G上的错误拒绝可观察；当前真实审查后果未知。
7. 是否提出下一项Core修改：不提出。没有新的真实效果证据，生产代码继续冻结。

本轮停止在附件规定的无授权离线交付边界，不扩大任务、追跑成功、借历史预算或输出成功率/对齐百分比。
