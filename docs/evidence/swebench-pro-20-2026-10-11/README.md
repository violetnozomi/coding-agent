# SWE-bench Pro 20 题：任务与记录要求

状态：**主模型请求已取消额外输出限制，9 题已结束并归档，其余 11 题正在首次接续**。截至前两阶段关闭，已返回 usage 的保守峰时估算 11.255294 元，剩余约 38.744706 元；当前阶段新增费用另在实时账本记录，估算不是账单。各阶段旧账目和原始记录保留，见末尾续接说明和 [逐题结果](current-outcomes.json)。

## 本轮目标

- 使用 NZ-Coder 原生 Agent 完成 20 道 SWE-bench Pro 任务。
- 每题保留执行记录，包括失败、超时、取消和环境阻断，供复盘和面试学习。
- 原有 Verified/SymPy 受控校准不计入这 20 题。
- 冻结版本、题目清单与资源边界后运行；不根据结果换题，不追跑到成功。

## 启动前的入口与阻断（历史准备记录）

开始时 NZ-Coder HEAD：`98171657a6d2be083fed2d39c74f80138ddb98e5`，`main` 与远端同步，工作树干净。

`nz_coder/swebench/profiles.py` 目前仅注册 Verified/Lite；现有 `run_harness` 调用 `swebench.harness.run_evaluation`。它们不能仅换数据集名称便被称为 Pro 的官方评分入口。

2026-10-11 查询官方仓库 HEAD：`66f92766bba642462d4bbe5479e83f91f9211862`。这仅是查阅版本，不表示已下载、安装或执行评分器。

官方 [总说明](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/README.md) 区分 V1 与 V2；[V2 流程](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/README.md) 使用 Harbor 任务、隔离推理及新沙箱补丁复评。V1 脚本不是 V2 评分入口。

- Pro 版本：用户已选择 **V2 公开集**。[20 题名单](selected-ids.txt) 按公开任务 ID 的固定哈希顺序抽样，不参考答案、难度字段或成绩。
- 本轮 20 题总费用授权：用户回复 **“50元，deepseek-flash”**，即人民币 50 元，包含主模型及全部辅助调用。按用户明确请求名 `deepseek-flash` 记录，不沿用历史预算。尚未发送模型请求。
- 模型与单题请求、时间、输出上限：尚未冻结。
- 本机 Harbor：未安装；已有 swebench/datasets 不等于 Pro 评分环境已就绪。
- 本机可用磁盘：初查 `1,842,569,216` 字节，随后仅约 130 MiB。[首题镜像核验](image-preflight.json) 只读取匿名注册表 manifest：压缩层合计 `971,831,312` 字节，超过当时可用空间，未拉取镜像。解压、运行及完整记录还需要额外空间。
- 当前状态：**blocked: insufficient_persistent_storage**。用户授权只读盘点 `pyh` 目录；未删除缓存、历史证据或用户文件。目录盘点保留在私有证据位置，不公开无关项目路径。
- 本次新增付费请求：0。20 题均为 `not_run`，官方 resolved 为 `null`；这不是 20 次失败或 20 次尝试。

[任务 manifest](task-manifest.json) 与 [SHA-256](task-manifest.sha256) 保存固定题目、配置和题面哈希。20 份公开题面及 `task.toml` 原件留存于受控目录 `/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11/public-tasks`，均逐文件核对固定提交的官方 `v2/SHA256SUMS`。没有下载答案或隐藏验证正文。

官方 API 目录查询遇到 403 限流；改用官方校验和文件。raw 文件直连超时后，仅下载命令使用已有代理。镜像 manifest 经直连获取。上述准备网络请求不是模型调用。

## 每题必须保留的证据

复用现有请求记录、AttemptJournal、Session/Artifact 归档与补丁冻结机制；不以裁剪后的模型展示冒充原始输出。

1. 任务 ID、官方版本、公开题面、初态提交/哈希、执行环境与实际模型配置。
2. 主模型及全部辅助请求/响应，工具参数、实际执行结果、权限记录、完整可恢复失败证据与产物引用。
3. 实际测试命令、范围、退出状态及对应文件版本；运行终态与未知项如实保留。
4. 最终冻结补丁、文件 diff、哈希、独立官方复评的原始日志与结果。
5. 请求数、真实 usage、费用已知/未知项、耗时、人工干预与阻断原因。
6. 一份简短复盘：问题定位、修改依据、失败与恢复、复测结论、可用于面试讲解的知识点。不得把受控回放写成模型自主能力。

原始记录在受控位置保留；仓库同步脱敏的可审阅轨迹与证据索引，不公开密钥和私有 reasoning 内容。空间或预算不足时停止下一题并登记状态，不静默删记录。

## 交付口径

最终目录逐题对账，分别报告已尝试、未运行、环境阻断、Agent 结束状态及官方 resolved 结果。20 题是计划规模，不是当前已完成数量；未运行和缺失评分不能当成通过。

## 准备完成与正式运行口径

用户已授权清理三项可重建缓存，释放 7.58 GiB；没有清理其他项目或历史轨迹。首题官方镜像已真实拉取，其项目 Python 3.11.2、Go 1.24.3。Agent 依赖按原有锁定的 38 个版本、对应 ABI wheel 校验值安装；独立复评使用未加 Agent 依赖的原始镜像。

用户明确接受经验编码器计量风险：“接受，按现有止损机制启动”。预算仍为人民币 50 元，旧预算不沿用。现有 RelayLedger 的预留、缺失 usage/超预留停止机制不变；只增加本轮 Pro V2 20 个固定实例和 `deepseek-flash` 授权识别。官方当前模型名及价格依据为 [DeepSeek 价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：按较高时段输入 2、缓存输入 0.04、输出 8 元/百万 token 保守估计，估计不是账单。

真实入口为现有 `NativeSDKRunner → AgentRunner → ToolExecutor`；全部主/辅助调用经宿主现有 UDS 转发账本。每题主请求上限 40、辅助上限 6、3000 秒、单请求输出 8000；总请求上限 920、输入 1500 万、输出 250 万。模型 `deepseek-flash`，非流式，thinking/effort 未显式设置；实际出站字段和响应模型另存，不猜供应商默认。任务使用原生循环/工具/默认 policy；工具暴露为已有 SWE 允许集合，Shell 允许本地 Go/Node/Python 等项目工具。这是显式实验权限条件，不代表完全无约束生产默认。

Agent 使用无网络、非 root、只读容器根目录、独立 HOME 和工作区。真实密钥、私人配置及官方验收脚本不进入 Agent。仅本题隔离 `.bash_profile` 恢复镜像 PATH，避免登录 Shell 丢失 Go 路径；没有修改用户全局配置。任务补丁冻结前停止整个 Agent 容器；官方 `test.sh` 在新原始镜像上运行，校验值核对，验收文件不泄露给 Agent。

现有归档工具保存投影轨迹，另保留原始容器 Session、工具 Artifact、状态和权限的校验归档。HTTP 请求/响应原件分别保存，不能把投影当成完整请求。仅归档成功后回收本轮工作区/缓存及本轮镜像标签，不全局 prune。剩余空间低于 512 MiB 会停止正在执行的 Agent，下一题低于 1 GiB 不准入；未下载镜像先按公开层大小作保守空间检查，估计不是解压精确保证。

本次离线检查：真实首题容器中读取 `go.mod`、实际 Bash 返回 `go version go1.24.3 linux/amd64`、生产 SDK 正常终止、归档重新打开通过；3 次本地受控模型请求，0 次付费请求，不计入 Pro 成绩。首次离线检查的 Go PATH 失败及归档路径失败原件保留，不改写。相关计费回归 72 passed、3 skipped；指定实际 tokenizer/recipe 资源后计数回归 7 passed。跳过项是未选实际资源的 2 个编码测试及 1 个旧生产审查资源集成测试；本题实际容器入口另行跑过。未运行 Windows 或全仓测试。

执行命令：`python docs/evidence/swebench-pro-20-2026-10-11/run-pro.py --execute`。每题结果写入私有 `live/<序号>/report.json`，总状态 `live/progress.json`。未运行、环境阻断、Agent 状态、实际请求及官方 resolved 分开记录。受控 smoke 不证明真实模型编码能力。

## 运行中的首题检查点

第 01 题 Agent 正常结束，官方独立复评通过。真实请求 16 主/0 出站辅助，usage 峰时估算 0.1620748 元；2 次辅助调用本地拒绝另记，不冒充真实模型调用。[逐题轨迹和复盘](attempts/01/README.md)已保存，完整 Session/Artifact 与未公开 reasoning 的原始响应仍保留在私有目录。其余题目正在按原名单执行，20 题未完成。

首次正式启动在镜像 OCI 格式预检处失败，未准入过任何模型请求；旧启动与账本保留并核验为零请求后才重新启动。修复的是镜像预检格式，没有重置付费占用或重新尝试已运行的任务。

## 运行中发现的项目依赖隔离修正

第 02 题仍运行时，实际原始镜像显示 pytest 7.4.0；Agent bootstrap 的 PYTHONPATH 带入 /opt/nz-site 后子进程导入 9.0.3。独立容器反例证明清除仅供 Agent 启动的 PYTHONPATH 后，子进程恢复 7.4.0，Agent 已加载的模块不受影响。native-entry 对后续题目作此环境修正，不更改文件工具或生产循环，不重跑已付费题目。第 01/02 题仍保留旧入口的逐字原件；第 02 题的推理阶段有此环境差异，不能作为无环境混杂的 Core 成绩。所有官方独立复评始终使用原始镜像，并未带入 Agent 依赖。每题入口副本/哈希用于区分这一修正，不能声称 20 题的 bootstrap 完全相同。

## 官方验收环境校正（不重跑模型）

第 02 题的首次验收禁用了 pytest 插件，官方镜像的 PYTEST_ADDOPTS 含 `--reruns=3`，因此报 unknown argument，必需测试一个也未执行。该 reward=0 是验收环境阻断，不能当作补丁功能失败。原日志和结果不改写；对同一补丁在同一冻结原始镜像重新运行 unchanged verifier，恢复官方镜像自身的插件加载，不增加模型调用。

当前长运行协调进程已经加载旧 grader 函数，修改脚本不会悄悄改变它；最终必须逐题核对官方原始日志，对受上述参数影响的验收做独立纠正。新脚本移除 grader 的 PYTHONPATH/PYTEST_DISABLE_PLUGIN_AUTOLOAD 覆盖，只隔离 HOME，并保留旧事件。未经有效复评的 Python 阻断结果记 unknown，不能按 failed 计入功能结论。

## 本次停止检查点

固定清单不换题。实际已尝试 01、02、03、06 四题；04/05 为准备阻断，07—20 为 `not_run: accounting_stop`，不能把后者当作模型失败。完整原始协调器 [summary](initial-coordinator-summary.json) 不改写；02/03 的有效官方复评在 [current-outcomes.json](current-outcomes.json) 和各题 `task-outcome.json` 中覆盖首次验收环境阻断的**解释口径**，并保留原日志。

| 题号 | 实际主/辅助请求 | Agent 终态 | 官方固定验收 | 限制 |
|---|---:|---|---|---|
| [01](attempts/01/README.md) | 16/0 | completed | required tests 通过 | 原 bootstrap；实验权限拒绝记录保留 |
| [02](attempts/02/README.md) | 40/0 | completed | 同一冻结补丁纠正验收环境后，6/6 必需项通过 | 推理阶段旧 bootstrap 的 pytest 路径混杂；未重跑模型 |
| [03](attempts/03/README.md) | 36/0 | completed | 同一冻结补丁纠正验收环境后，6/6 必需项通过 | 同次全文件运行仍有 1 个非计分测试失败，不能称整个测试文件全绿 |
| [04](attempts/04/README.md) | 0/0 | 未执行 | unknown/null | 项目 Python 3.8 无法直接安装 Agent 的锁定依赖；隔离方案已离线验证，尚未付费补做 |
| [05](attempts/05/README.md) | 0/0 | 未执行 | unknown/null | 5.79 GB 压缩镜像拉取超过准备阶段 420 秒；不是 Agent 3000 秒超时 |
| [06](attempts/06/README.md) | 11/0 | error | 失败 | 三次 length 后既有输出恢复耗尽，未提交修改；独立 Go 验收真实失败 |
| 07—20 | 0/0 | 未执行 | unknown/null | 全局计量止损；每题单独保留状态目录 |

总计 103 个真实主请求、0 个真实出站辅助请求；本地辅助准入拒绝另记。所有真实请求都返回可核验 usage，峰时估算 3.52470736 元，50 元未用完。预算不足、计量止损与模型失败是不同事实。没有算两任务或 20 题成功率，没有运行 InfCodeX，没有人工修改题目补丁。

第 06 题第 11 次实际出站 `max_tokens=8000`，输出预留 8000；Provider 报告 `completion_tokens=8001`，input 未超预留，finish_reason=length。现有账本记录 `accounting_contract_failed`，此后没有新的 admitted 事件。多出的 token 原因未知，不能猜成 EOS 或供应商 bug。[止损证据](accounting-stop-audit.json)、[完整账本](ledger.jsonl)包含请求哈希和原 usage；公开响应已移除私有 reasoning_content。

用户之前明确接受“缺失 usage 或超预留就停止后续调用”的机制。本次不重置账本、不放宽守卫、不借剩余额度自动续跑。已准备的补做入口在账本失败后实际拒绝，保留 [拒绝记录](continuation-rejection.txt)；未创建第二阶段账本，未发送补做模型请求。

## 已授权的隔离 Agent Python

用户明确选择“补隔离 Agent Python（建议）”。[环境与零付费证据](isolated-agent-python/verification.json)：从本机现有 Conda Python 3.13.12 复制解释器、标准库与 20 个必需共享库，排除私人 site-packages、pyc 和缓存；38 项 Agent 依赖沿用原锁定版本，下载相应 cp313 wheel 并记录 SHA-256。依赖下载与模型出站分开，Docker 构建使用 `--network=none`，没有 sudo、替换系统 Node/Python 或修改全局配置。

真实第 04 题原始镜像为 Debian 12 / Python 3.8.20。在这个镜像派生的隔离容器中，真实 NativeSDKRunner、文件工具读取 setup.cfg，真实 Bash 返回原项目 Python 3.8.20 和项目 pytest 路径，最终 completed，未改文件；3 次模型请求全部在回环受控 Provider，0 次付费。Agent 显式由 `/opt/nz-agent/bin/python3.13` 启动，项目 PATH 不切换，独立官方验收仍用原始镜像。没有声称第 10 题实际工具链已运行。[构建配置](isolated-agent-python/build-config.json)、[源码身份](isolated-agent-python/source-identity.json)、[实际模型请求](isolated-agent-python/provider-requests.jsonl)、[工具轨迹](isolated-agent-python/runtime.jsonl)可核对。

首次隔离 smoke 在 Unix socket 路径长度处阻断，尚未触达 SDK/Provider；原日志留存。缩短本轮目录后实际入口通过，不把首次失败删除或改成成功。

同时发现 Agent 启动用的 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 仍会影响项目子进程。本轮仅在 Agent 导入后移除这一覆盖和原已移除的 PYTHONPATH；第 06 题与成功隔离 smoke 留存 harness_revision=3 事实。第 03 题的 revision=2 不追认使用新设置；第 01/02 题 revision=1 不追认修正。对照不是无环境混杂的固定工具链实验，不据此归因整体 Core 能力变化。

本轮停止于已保存原始轨迹、冻结补丁及有效/未知项。继续 16 道尚未尝试题之前，须解决并确认超预留停止后的处理口径；已尝试四题不自动重跑。


## 用户授权续跑与主请求默认输出

用户在 8000/8001 停止后明确回复“继续进行”，随后要求“你别设限制行吗？”。当前阶段主请求省略 `max_tokens`，由 Provider 采用其默认值；这不是无限输出。官方 [请求参数](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/) 列出的最大输出容量 393216，仅用于宿主费用预留，不写入模型请求。实际 thinking/effort 仍未设置，保留非流式。Core 内部输出预留 65536、上下文容量 1000000；这与原阶段显式 8000 的条件不同，不按同配置对照解释成绩。

总授权仍为人民币 50 元。原阶段实际 usage 的保守峰时估算 3.52470736 元不清零，新账本仅承接剩余 46.47529264 元，且核验旧账本哈希、唯一结算与具体 8001 事件。未尝试的固定题号为 04、05、07—20；已尝试的 01、02、03、06 不重跑。不追加收费授权，不自动重复失败 Agent。费用未知、缺失 usage、超官方容量或预算不足仍停止后续调用。

[回归结果](provider-default-output/test-results.txt)：85 passed、3 skipped；跳过项是未选择完整资源的旧审查测试和两个计数资源测试，没有自动下载或调用收费模型。真实 SDK 的受控检查在 Ansible 原始项目 Python 3.8 环境读取与执行命令，3 个实际主请求均没有 max_tokens，0 个收费请求；[入口检查](provider-default-output/runtime-smoke-verification.json) 和 [账目续接检查](provider-default-output/continuation-verification.json) 留存。初次归档重用了旧 smoke 名称而冲突；未重跑工具，改为独立归档名保存同次执行证据。

运行命令 `python docs/evidence/swebench-pro-20-2026-10-11/continue-unattempted.py --execute`。新原件位于私有 next 目录，公开记录逐题放在 continuation-attempts。原先的 [首阶段结果](initial-outcomes.json)、原账本和原停止证据保持不变；最新汇总只替换已经有新报告的未尝试题目。每题终止、功能验收、usage、辅助拒绝与环境阻断分别记录。仍保留原任务轮次、安全、隔离和记录边界；本次只移除人为的主请求输出上限。


## 第一次默认输出阶段的关闭与环境续接

179 次真实主请求、0 次出站辅助请求均已结算；与首阶段 103 次合计 282 次。累计已返回 usage 的保守峰时估算 11.255294 元，剩余 38.744706 元，不是账单。04/07/09/10/12 五题的官方必需项通过；04 与 12 的 Agent 仍为 max_turns，其他三题正常结束。第 12 题必需项 42/42 通过；不反写 Agent 状态。

08/11 在 manylinux 与 musllinux 轮包匹配处准备失败，均未调用模型；05 空间预检失败，也未调用模型。源文件修复不会改变已加载的长进程，因此在第 12 题完成验收与归档、所有已准入请求已结算后，仅终止本次协调器及其第 13 题 docker pull 子进程；原账本不改写。13—20 的零次尝试记录据实际账本登记为环境准备重启，不伪造 Agent 运行或通过。第一次检查发现 12 已有真实请求，守卫拒绝重启并恢复原进程；这次短暂暂停/恢复也留存，不隐藏人工干预。

现有入口 `continue-unattempted.py --preparation-retry --execute` 只接续 05、08、11、13—20 的首次 Agent 尝试。预算从两份已关闭账本逐笔扣除，前阶段未知 usage、未结算请求、计量失败、费用矛盾或已发请求却声称零次尝试，都会拒绝新调用。新增离线回归 9 passed，不调用模型；[测试](../../../tests/evaluation/test_pro_continuation_plan.py)。主请求继续省略 max_tokens，50 元总授权不变。

当前已有的大镜像优先使用后回收，再准备缺失镜像，固定名单和题号不变；排序只依据镜像缓存，不依据成绩。只清理本轮构建日志可归属的失败容器/中间镜像，以及已归档的离线 SDK 临时镜像，不清理用户或其他项目资源。原阶段闭账与人工干预见 provider-default-output 中的 phase2-summary、phase2-ledger 与 coordinator-preparation-restart 原件。

第 08 题真实第 9 次主请求没有 max_tokens/max_completion_tokens，Provider 报告输出 36287 token，finish_reason=tool_calls，账本正常结算。见 [原始身份与 usage](provider-default-output/observed-long-output-36287.json)。这不是功能通过证据；该任务仍在运行，长响应后的搜索耗时保留在轨迹。第 07/09/10/12 题的 learning.md 补充冻结补丁对应的复盘要点，不向正在运行的 Agent 提供提示。
