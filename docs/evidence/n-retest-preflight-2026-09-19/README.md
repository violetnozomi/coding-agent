# 0885c87：真实 N 复测前检查（未执行真实模型）

**真实 N 复测尚未执行。** 当前会话没有新增付费授权；历史 40 次请求不构成本轮许可。真实主模型、辅助模型、embedding、在线评测均为 0，未重跑 InfCodeX。没有启动历史付费代理、读取 API 凭据或修改实现、测试、Runner、提示词和权限策略。本目录只保存预检、离线 smoke 和待采集字段。

## 1. 配置

| 字段 | 已核对事实 / 后续复测约束 |
|---|---|
| NZ commit | `0885c874b949792723c515ff098c24d31cd238c9` |
| 仓库 | 开始时 main 工作树干净；HEAD、origin/main、`git ls-remote origin refs/heads/main` 相同；ahead/behind = 0/0 |
| 初态 | `docs/evidence/autonomous-comparison-2026-09-15/initial/N` 七文件逐字节复制；与历史 N initial-hashes 全部相同 |
| 重建目录 | `/tmp/nz-n-preflight-0885c87-20260919/initial`；无 Git、无依赖安装、无补丁 |
| 模型 / Provider | `deepseek-v4-flash` / `openai-compatible` |
| 入口 | 原 `prompt.build(memory_block="", skill_descriptions="")` → `build_product_run_environment` → `NativeSDKRunner` → `AgentRunner` |
| 主请求上限 | 12 个实际主请求；不得把重试或辅助调用混入此计数后隐去 |
| 输出限制 | 历史出站 `max_tokens=64000`；新运行尚无出站请求，不能宣称已验证新运行字段 |
| thinking / effort | 原请求两字段均未设置；不解释为 medium 或供应商默认等价 |
| stream / retrieval | stream=true；保留生产默认 retrieval，历史 effective 为 null |
| 权限 | permission_mode=auto，保留原 `run-nz.py` permission callback |
| 网络 | 原 Linux seccomp 禁止 IPv4/IPv6 socket 和 io_uring；工具继承限制；模型仅经 Unix socket → 获授权代理 |
| 辅助模型 | 保留正常 semantic verifier；默认继承主模型，显式 provider/model 成对配置才覆盖；主/辅助均无本轮授权 |
| Node / Python | 历史 Node v24.18.1；Python 3.13.12，路径和 Node 二进制 hash 见 environment.json |

原任务全文（未改写）：

```text
Make escaped literals usable with new RegExp(..., 'u'), including hyphens. Preserve ordinary literal matching and rejection of non-string inputs. Run node --test literal.test.cjs and report the change and verification.
```

原权限回调先移除 `2>&1` 再检查，允许 bash 的 pytest、python/python3 -m pytest、node --test 前缀，可带限定 head/tail 管道；拒绝 `;`、`&&`、`||`、反引号、`$(`、`>`、`<` 等。允许执行管道不表示其 shell exit 0 是可信验证通过。以原文件为准，不扩大为任意 Bash/process 权限。runtime-owned project verifier 的授权面另行记录，不混同 permission denied。

## 2. 新 N 逐请求轨迹

没有实际请求，因此不填造 1–12 行，也不把离线替身响应计作真实 deepseek 请求。

| 实际主请求 | trigger | 请求前剩余预算 | finish_reason | 工具及执行事实 | generation / contract / semantic | Provider usage |
|---|---|---|---|---|---|---|
| 无 | 未启动 | 未消耗（计划上限 12） | N/A | N/A | N/A | null |

请求前余额按包含本次请求计算：若无额外主请求或重试，第 1 次为 12，第 12 次为 1；请求后余额另列，避免沿用旧表的 after 口径。每次模型可见消息与实际 tool-result ID 分别记录，不能用 Runtime 内部状态代替模型已经看到的内容。字段见 [expected-capture-fields.json](expected-capture-fields.json)。

## 3. A–F 边界

| 边界 | 历史 NZ N | 新真实 N | 新样本证据强度 |
|---|---|---|---|
| A 正确补丁形成 | #7，generation=1 | 未执行 | 无 |
| B 声明测试通过 | #8，真实 3 pass / 0 fail | 未执行 | 无 |
| C Runtime trusted verification | #9 模型看到输出，但 Runtime 未绑定 | 未执行 | 无 |
| D 已知完成条件齐备 | 全部自然语言需求是否满足未知 | 未执行 | 无 |
| E 首次合法完成边界 | 原运行没有 terminal acceptance | 未执行 | 无 |
| F 实际结束 | #12 后 max_turns；所有响应为 tool_calls | 未执行 | 无 |

新 N 最终 acceptance、terminal status、workspace diff 和最终源码 hash 都为 N/A。初态 hash 与离线 smoke 的 hash 不能冒充新 N 最终源码 hash。

## 4. 生产证据链 smoke 与旧 N 对照

已有六组相关回归：**320 passed，0 skipped，0 failed**，见 [smoke-tests.txt](smoke-tests.txt)。真实工具/Node 执行，模型和 semantic verdict 使用已有离线替身，外层 seccomp 禁止 Internet；不提供真实模型行为改善证据。

已有 paid-N action fragment 本次仍产生 failed → passed，当前 `mutation_generation=acceptance_mutation_generation=verification_generation=contract.attempted_generation=1`，command 为精确 `node --test literal.test.cjs`、scope 为 literal.test.cjs；`requested_paths=[]`，测试文件不产生修改义务。受控 semantic accept 后，ledger 满足，semantic_review_generation=1，已有 early completion 正常结束。见 [offline-binding-check.json](offline-binding-check.json)。其中 contract.source=model 表示由模型发起的真实工具执行，不表示相信模型自报结果。

这回答了“原修复的接入在离线生产链中是否仍存在”：是。它不能回答“真实模型是否利用”“首次合法边界是否提前”“新的第一阻塞点是什么”：均未观察。

已有反例继续通过：新 mutation 使旧验证失效；B 型旧测试绿但新 API/caller 未改不能完成；缺显式 README 不能完成；权限拒绝无真实执行，不能通过；缺依赖为实际 command failure。命令分类/契约已有管道、`|| true`、scope 不同、越界和 unsupported flags 检查。补充 smoke 中 echo、自报 pass、node -e、help/version 不被分类为测试；掩盖失败的 shell 组合不能形成可靠成功。

**预检不能全部通过：零测试 / 全 skipped 限制。** 本轮补充 smoke 使用独立临时 fixture，未修改历史任务及其测试。在真实 Node 子进程下，空 suite 输出 tests=0/pass=0；all-skipped 输出 tests=1/pass=0/skipped=1。两者 exit=0，当前 `verification_output_has_no_tests` 均返回 false；生产 Runner 将 contract 记为 passed，并在受控 semantic accept 后 completed。详见 [supplementary-smoke.json](supplementary-smoke.json)。另一个空文件样本由 Node 自身计为一个通过的文件级测试（tests=1/pass=1），不能与零测试混为一谈。这些是离线执行事实，不是新真实 N 的失败轨迹；没有做旧 commit 差分，因此不能断言由 0885c87 新引入，不能认定是原 N 或未来 N 的第一阻塞点。没有修改 Core，也没有用绿色回归概括为“全部安全性质确认”。

cancelled/timeout/unknown、跨 run/跨 workspace 注入、显式 typecheck 与缺失 optional typecheck 的完整 Node 端到端负例，本轮没有新增证明，标记为**未完整确认**。已有片段证明当前 scope 和当前 generation 正例，以及 mutation 后失效；不能由此外推所有拒绝矩阵。后续若进入修复，需单独建立失败回归并证明最小修改，不以此 smoke 发起多问题重构。

InfCodeX 固定 `d3a812379b589597347f5be12d5b68477e577f02`，仅为 **historical paired reference / 非同时重跑样本**。历史同样 12 个主请求，经历基线失败、错误 edit、重读、write 修复、Node 测试、问题扩展断言及修正、git diff 失败、再次 Node 测试，第 12 请求 stop。不能写成新同期 A/B，也不能写“测试一过立即结束”。

## 5. 独立验收与环境就绪程度

重建初态真实执行 `node --test literal.test.cjs`：exit=1，2 pass / 1 fail，失败为 Unicode 连字符 Invalid escape；与历史初态相符，执行后七文件 hash 不变。见 [initial-state.json](initial-state.json) 和 [initial-node-test.json](initial-node-test.json)。这只是初态核对，不是最终 acceptance。

后续独立验收必须对新运行冻结副本分别执行原公开 Node 测试与原独立断言（Unicode literals/metacharacters/non-string rejection），存 exit/stdout/stderr、文件及 SHA-256。原独立断言不会证明所有 character-class embedding 兼容性，不扩大验收覆盖面。

真实运行目前 `execution_ready=false`：除付费授权缺失外，原 `/tmp/paid_pair_compare.py` 会自动执行两侧，代理为每侧合计 16 请求限制，并原样落盘响应（可能含 reasoning_content）；**不可直接复用其 main 执行**。旧 `run-nz.py` 也不保存本轮要求的所有逐请求状态快照。这里只登记后续采集要求：NZ 单侧、12 实际主请求、独立辅助计数、只对日志副本过滤 reasoning_content、不改变 live 模型请求、完整 RunEvidence/ledger/generation/snapshot。未新建付费 Runner 或代理。原入口与隔离器源码 hash 见 [harness-source-hashes.json](harness-source-hashes.json)。真实上游 endpoint 的本轮有效连接未验证；未读取凭据，更未发送探测请求。

## 6. usage

本轮真实主请求 **0**、真实辅助请求 **0**、embedding **0**、在线评测 **0**；真实 Provider usage 为 **null（未执行）**，不使用离线测试中的固定 usage 计算效率。费用：**cost unknown**；未产生本轮模型请求也不代表供应商服务免费。后续逐请求保留 Provider 原 usage 字段，cache input 不重复加到 prompt total，不从 runtime cost=0 推算美元。

## 7. 结论强度及停止

- **强证据：** HEAD/远端一致；历史初态七文件一致；初态失败可复现；320 项已有离线回归通过；当前精确 Node 验证在真实工具链进入 generation=1；补充零测试/all-skipped smoke 存在误记 passed。
- **中等证据：** 现有正向链具备支持真实复测的本地基础；尚未证明采集器满足完整记录和实际主请求硬上限。
- **证据不足：** deepseek-v4-flash 是否改变自主轨迹、是否正常结束、首个新阻塞点、成功率或效率改善；部分完整拒绝矩阵；负例是否由本提交引入。

按用户的未授权停止条件，到此停止。没有新的真实轨迹，因此不进入 Core 修复；不请求沿用历史许可，不自动重跑。
