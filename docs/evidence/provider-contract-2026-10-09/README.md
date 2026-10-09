# Provider 计数契约与正式入口收尾

本阶段出口为 **B：样本一致，但严格输入上界未获证明，在线停止**。已修复超预留后继续准入，接通正式旧/新入口；没有新的费用授权，远程模型请求为 **0**。生产 Core、冻结任务、既有付费记录均未修改。

开始时 HEAD 为 `604ece0599bbea1381d683eab1d375def3151425`，工作树干净，无后续增量。冻结材料为 `91bf79826c39733e606f3f694ebbc7970eba1356`，其 manifest SHA-256 仍为 `ce3533973916c837fdc7e6c9827af9eb3d793e6d5c76684dbb942a8d6612e994`。被测旧 Core `cf2ff5cf078559e9843c34614318d80c984b4168`、新 Core `4abbcafae69f2e3b9d162ad73e769913659aca59`。InfCodeX `d3a812379b589597347f5be12d5b68477e577f02` 仅作历史参考，本轮未启动。

| 项目 | 已证明 | 待证明 / 本轮修改 | 验收 |
|---|---|---|---|
| 隔离 | 复用 Docker、38 个锁定 wheel、运行包、三个隔离回执 | 正式入口按单元选择旧/新包；启动前核对全部挂载 | 5 个实际容器、代码 SHA、初态 SHA、独立 HOME、network=none |
| 输入计数 | 官方参考编码可运行；23 个 NZ 历史请求计数一致 | 托管服务模板/资源版本/隐式开销未获一般性保证 | strict 仍拒绝 unknown/empirical；payload hash 绑定 |
| 超预留 | 原实现丢弃有效超额，可能继续放行 | 保留报告值、差额、契约、请求身份；原子停止实验后续角色 | 真 Unix relay + 假 HTTP 上游、并发屏障、重启拒绝 |
| 正式运行 | 真实 Runner/Judge 入口本地可达 | status/online 从独立条件计算，不使用 ready 开关 | 原生 u03 零请求；无授权/未知计数拒绝；充分条件分支单测 |

## 当前服务及参考计数

2026-10-09 核查的 [首次调用说明](https://api-docs.deepseek.com/)、[更新记录](https://api-docs.deepseek.com/updates/) 和 [模型与价格](https://api-docs.deepseek.com/quick_start/pricing/) 一致说明：`deepseek-v4-flash` 是退役旧模型的兼容名称，当前由 V4.1-Flash 服务。这个结论来自官方映射，本轮没有 API 响应来独立核验当次服务。历史报告不改名，保留请求 alias、当时响应 `deepseek-flash` 和 fingerprint。

[9 月 10 日公告](https://api-docs.deepseek.com/news/news260910/) 与更新记录关于 **V4-Pro** 的 9 月 14 日迁移口径冲突：前者称转入 V4.1，后者称 Pro 继续服务。保留此差异；Flash 映射没有该冲突，不选择旧 V4 tokenizer。

参考实现调用官方 `ChatCompletionRequest.convert` → `DeepseekV41Encoding.encode`，包含完整角色、工具定义/参数 schema、历史调用及结果、thinking、effort、reasoning 和生成前缀。没有把 HTTP JSON 字节当 DeepSeek token，也没有比例或安全系数。[官方源码](https://github.com/deepseek-ai/deepseek-recipe/tree/57b9c842429d850b03b5fdbe3fed266c13cc7f2b)、[tokenizer 用法](https://github.com/deepseek-ai/deepseek-recipe/blob/57b9c842429d850b03b5fdbe3fed266c13cc7f2b/docs/tokenizer.md)

| 资源 | 身份 |
|---|---|
| 参考源码 revision | `57b9c842429d850b03b5fdbe3fed266c13cc7f2b` |
| V4.1 tokenizer SHA-256 | `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99` |
| 官方 wheel | PyPI deepseek-recipe 0.1.1；内部 Cargo/native 版本为 0.1.0，公开 pyproject 解释两者独立 |
| wheel SHA-256 | `7818802935becc76ac9b81af6b4a1e5390ec65355cecefd1fd4790f51845c1c5` |
| 实际 native SHA-256 | `1461bc4895c188d4ad60ca05eb7bd663aa7fa3286f2ecbebca39ecd73c5b0ec1` |

wheel 不附构建 Git revision，故源码 revision 记为参考来源，不声称已证明二进制与整份源码逐字对应。实际资源及 counter 源码 hash 进入契约身份/状态。只下载必要 tokenizer、源码、文档及 11.4 MB 官方 wheel，放在本轮私有目录；未安装到全局环境，未取模型权重，未调用 remote code 自动加载。公开文件只发布来源与 hash，见 [resource-hashes.json](resource-hashes.json)、[官方文档快照身份](official-doc-sources.json)。GitHub raw 直连超时后使用已有代理；其余成功直连，未改全局配置。Hugging Face 元数据直连超时，所需资源改取官方 recipe 已明确标注的 V4.1 文件。

支持范围是本实验的纯文本 Chat Completions，模型限 Flash 两个名称。未知字段、结构化图片/文件引用、额外请求能力和未核实 effort alias 明确拒绝；不支持的字段不删除。当前不支持 `max_completion_tokens`。计数绑定最终序列化 payload 的 SHA；计数之后的参数变化被拒绝。四级字段是 exact / proven_upper_bound / empirically_calibrated / unknown，严格准入只接收前两类且必须来自宿主可信 counter。

[历史对照](historical-counts.json)复用原 B/N 40 个请求：NZ 23 个均可参考编码，23 个 delta=0；包括 22 个流式主请求、一个 thinking=disabled 的非流式审查（5887 对 5887）、工具 schema、真实工具结果历史和代码/Unicode。17 个 InfCodeX 请求因 `max_completion_tokens` 请求形状不支持，参考值为 null，未转换字段凑一致。两侧响应观测为 `deepseek-flash`，fingerprint 均为 `aeb56401ca74e127821c4f9126dcb669`。原记录保存语义 payload，未保存原始请求 HTTP 字节；分析 hash 是重新序列化的分析载荷，不冒称原 wire hash。

这些结果只是有限历史样本的经验支持。托管 API 未明确承诺使用上述资源、转换选项及完整模板，也未承诺无额外服务内容/开销。缺的不是“再乘一个系数”，而是**服务端口径等价或限定形状的上界证明**。[官方 token 用量说明](https://api-docs.deepseek.com/quick_start/token_usage/)也将字符换算列为近似，以返回 usage 为准。`reference_count` 与 `upper_bound` 分离，实际 counter 仍给 unknown、trusted=false、upper_bound=null；经验结果不会自动启用线上准入。

## usage 与超预留

[当前 API 定义](https://api-docs.deepseek.com/api/create-chat-completion/)把 max_tokens 定义为生成 token 上限，reasoning_tokens 是 completion_tokens 的分项；因此本实验按完整 completion（包括 reasoning）预留，不能另加 reasoning。prompt 包含缓存命中/未命中，分项不再次累计。SSE 最后内容块携带整次 usage、随后 DONE，不假设另有 usage-only 块。重复累计值只结算一次，矛盾累计值保留冲突并停止契约；length 的有效 usage 仍记实际量，但不代表任务完成。aborted/error/断流/缺失/非法 usage 不释放未知消耗为零。客户端断开不证明供应商停止计费。

[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)要求有 tools 时回传既有 assistant reasoning_content；参考编码保留这一内容，不为了计数删除它。主任务 frozen thinking/effort **未设置**，不改写为显式 high；审查实际请求 thinking=disabled、max_tokens=1024、stream=false，见 [原始本地请求](formal/raw-provider-requests.json)。主输出 64000、审查 1024、总输出 100000 均未缩短。token/请求边界与费用授权、最终账单独立。

修复前的 [失败回归](over-reservation-before-corrected.txt)：输入总额 250、每次预留 100，第一次上游报告 150/7/157 后，第二次竟返回 HTTP 200。最初夹具被输出总额提前挡住，[原日志](over-reservation-before.txt)保留；改用每次输出 1024 排除这个干扰，再证明第二次放行，不把最初夹具当作完整反例。

额外用隔离导入的 604ece0 源码和当前源码重复同一真实转发反例，保留 [前后账本及上游次数](over-reservation-comparison.json)：旧为 HTTP 200/200、两次上游、报告输入 300、错误占用 200；新为 200/429、一次上游、报告/占用输入均为 150。它是模拟账本缺陷，不是线上超支。

修复后有效超额不是 invalid usage：reported_usage、预留、正差额、contract_id、attempt_id 都保留，占用按报告量更新（100→150），同一实验其他单元/角色不再准入。缺失/非法值保留预留；矛盾值保留 unknown/冲突，可能超额的报告增加保守占用，停止继续消费。结算和新准入共享原 RLock；等计数完成的并发请求在超额已知后被拒绝，上游只有一次。已准入请求保持记录，活动转发尽力取消；不能追认以前始终守住上限。原账本 O_EXCL 拒绝重启，新启动器也拒绝覆盖同一实验目录。

## 正式入口本地证据

复用上阶段镜像 `sha256:ad95168761bfd58549057442c026c24cb863a5bf2f3511c8f0bae5c462c49641`、Python 3.12.13、两套 355 文件运行包、锁定 wheel 和隔离回执；未重建镜像。runtime.json 中 driver hash 是初次准备版本，三个实际 probe 回执均绑定最终 604ece0 driver；此次按真实回执及该提交的 Git blob核验，未重写旧 metadata。新 driver 的全部挂载、版本、资源和 source hash 在容器 create 后、start 前检查，见各单元 launch/container-inspect。

| 单元 | 实际源码包 | 本地准备协议响应 | 真实上游请求 | 原生审查/结束 | 独立功能验收 |
|---|---|---:|---:|---|---|
| u01 G/新 | 4abbcaf | 3 | 审查 1 | accept/verifier_ok | 3/3 |
| u02 D/旧 | cf2ff5c | 3 | 审查 1 | revise/verifier_ok | 2/3，默认负数拒绝缺失 |
| u03 G/旧 | cf2ff5c | 3 | **0** | deterministic_compatibility_guard | 3/3；不绕过旧规则 |
| u04 D/新 | 4abbcaf | 3 | 审查 1 | revise/verifier_ok | 2/3，默认负数拒绝缺失 |
| a01 A/新 | 4abbcaf | **0** | 主请求 11、辅助 0 | 实际 Runtime error；预算拒绝后有本地重试，未新增上游请求 | 1/9；未实现新功能 |

固定候选由本版本 NativeSDKRunner → AgentRunner → 真实 read/edit/bash/pytest → 原生 StopHook 生成。准备只提供协议响应，不直接写文件/执行命令；每单元收到首个审查裁决后停止。审查请求不包含候选标签、隐藏验收、另一候选或预期裁决。A 不走准备分支，读取和真实既有测试经原生工具执行；假模型没有提供补丁，需求仍缺失，不能洗成完成。

[formal-results.json](formal/formal-results.json) / [ledger](formal/ledger.jsonl)记录本次正式回归的 14 个实际假上游请求，与独立假服务记录一致，输出测试值合计 98、输入测试值 481483（本地假服务定义的 JSON 字节 token）。这些数不能当作 DeepSeek token 或线上效率证据。首次 status 拒绝为 0；三次先行本地执行和表中最终回归各 14，正式入口测试合计 56 个本地假请求。relay 单测还各有独立假请求，均非收费请求，不复用正式账本。完整请求、原生审查包、工具事件、权限、状态、diff 和 [独立验收](independent-acceptance.json)在本目录；原始目录及逐文件对应 hash 见 [provenance.json](provenance.json)。无需从 exit=0 推断功能通过。

`technical_ready` 分开检查隔离身份、宿主 counter 模型范围/资源/源码、输出契约和预算；费用授权不参与技术 ready，online_allowed 额外核对账户、授权正文、时效、费用上限、模型/endpoint、冻结 manifest、counter 身份及请求/token边界。未提供计数资源时 status 返回明确阻断，而不是报成调用失败。ready JSON 本身不能替代三个隔离回执或计数证明；本地 fake 契约不能以 service_scope 之外的方式晋升线上。

当前 [status](status.json)：隔离 true、输出契约 true、输入契约 false、严格预算 false、费用授权 false、technical_ready false。实际 [online 拒绝](online-refusal.json)在密钥读取与执行之前发生，退出 2；执行还套用了既有离线网络封锁。充分条件的 online 分支有独立条件单测；它不是线上实际执行证明。主、审查及所有辅助路径都经私有 Unix relay，SDK 不自动重试；Gateway 原生重试仍逐次受准入约束。未开启 embedding/在线 fallback。

## 验证与停止

[最终回归](final-tests-v2.txt)：85 passed；随后增加的 [已在途请求保留/取消回归](inflight-test.txt) 单独 1 passed（生产代码未再改，未重跑已通过矩阵）。覆盖旧 relay、计数、超预留输入/输出、缺失/非法/冲突、缓存分项、SSE、取消、跨角色/并发/重启、payload 改动、正式容器和原适配器。另 [网络边界](smoke-boundary-tests-corrected.txt) 1 passed、1 skipped：`test_real_paired_entrypoints` 未启用 opt-in 的 InfCodeX 真实对测，符合本轮不启动范围。官方原样 rendering/encoding 测试 [2 passed](official-encoding-tests.txt)，在网络封锁下执行。ruff [通过](ruff.txt)，git diff --check 通过。命令见 [commands.md](commands.md)。未运行全仓、Windows、真实付费模型或历史 compaction 专项，不声称全仓通过；既有两项 compaction fake 历史失败留在原审计记录，未弱化或删除。

本轮唯一补证据方案见 [calibration-proposal.json](calibration-proposal.json)：先寻求供应商明确的托管模板/资源版本与开销契约；若另获校准授权，至多三个一次性请求用于量化样本差异，失败不重试、不执行生成工具。即便三个点都相等，也不能给普遍 exact；经验边界须另获用户明确接受，当前严格模式不自动降级。没有该授权，本轮不执行校准，停止在线工作，也不据此提出新的 Core 修改或自主编码成功率结论。
