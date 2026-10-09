# 实际进程隔离与宿主预算转发验收

本轮生产 Core 未改。复用 [冻结准备材料](../review-effects-preflight-2026-10-09/README.md)，完成了真实容器进程隔离和本地模型转发限额验收。线上请求为 **0**，G/D 与 A_unknown_location 均未运行。

| 门槛 | 状态 | 依据与限制 |
|---|---|---|
| isolation_verified | true | 新 Core 的真实 NativeSDKRunner、AgentRunner、ToolExecutor、Bash Python 子进程、pytest 和辅助 Judge 经过正式 Docker/Unix socket 路径 |
| budget_enforcement_verified | false | 本地转发契约通过；所选 DeepSeek 输入 tokenizer/可靠上界及 thinking 输出口径尚未核验，默认计数器拒绝请求 |
| paid_authorization_valid | false | 没有覆盖本轮模型、账号、范围和全部辅助调用的新授权；历史授权不复用 |
| technical_ready | false | 当前结果属于“限额仍有具体缺项”，在线入口退出 2，online_not_run |

32 passed、1 skipped 是离线边界回归结果。它不证明模型自主编码效果或 G/D 审查效果，不代表线上效率。假服务把完整请求 JSON 字节作为自己的输入测试单位、输出 usage 固定为测试值 7；这两个定义均不得用于 DeepSeek。

## 冻结内容与环境

证据基线 `91bf79826c39733e606f3f694ebbc7970eba1356`，开始时 HEAD 与之相同、工作树干净。旧 Core `cf2ff5cf078559e9843c34614318d80c984b4168`，新 Core `4abbcafae69f2e3b9d162ad73e769913659aca59`，各自裁剪出的 355 个生产源码文件有独立哈希表。原 manifest SHA-256 为 `ce3533973916c837fdc7e6c9827af9eb3d793e6d5c76684dbb942a8d6612e994`，未修改。四个审查初态及自主任务初态均从冻结材料恢复并核对哈希，未沿用已修改的目录。

InfCodeX `d3a812379b589597347f5be12d5b68477e577f02` 只作历史标识，本轮不启动它。正式顺序仍为 G/新 → D/旧 → G/旧 → D/新 → 一次 A_unknown_location。未用额度不转给其他实验。

原 `all_auxiliary_physical_request_cap=6` 在 [执行补充清单](execution-supplement.json) 明确为**自主任务辅助量**：四个审查单元各 2 次，合计 8；自主主请求 12、辅助 6；全实验 26。输入单请求 100000、总量 500000，输出总量 100000；主 max_tokens 64000，审查 1024；请求 180 秒，自主 900 秒。这些是未授权的提案，不是可消费额度。

只选 Docker 一个文件隔离后端，没有重试已知失败的 bwrap、改 sysctl、安全策略或使用 sudo。基础镜像为锁定的官方 Python digest，实际构建 image、38 个 wheel 哈希及命令见 [runtime.json](runtime.json)、[requirements.lock](requirements.lock)、[构建日志](build.txt)。依赖下载使用官方 PyPI，构建 `--network=none --pull=false`；下载和模型调用分别记录。此次两版共用 Python 3.12.13；上一阶段为 3.13.12，不能称环境完全未变。旧版实际隔离导入通过；第一次导入以 `/` 为 cwd 被既有 trust 校验拒绝，改用正式 `/workspace` 后通过，两个日志均保留。

## 运行边界与证据

复用已有 OpenAI SDK 的 Unix HTTP 通道和 NativeSDKRunner/Judge。现有 offline_exec 只隔离网络，不能提供本轮文件边界，因此正式探针由 Docker 承担。新增 [model_relay.py](../../../nz_coder/evaluation/model_relay.py) 只做宿主准入、转发与轻量账目；[启动脚本](../../../scripts/review_effects_execution.py) 不重建 Agent 循环或权限框架。

容器 network=none、根文件系统只读、无 capabilities、no-new-privileges、私有 PID/IPC、UID/GID 1001、128 个 PID 与 512 MiB 内存。仅当前工作区、私有 HOME/TMP/结果可写；运行代码、驱动、输入和模型 socket 只读。代码包排除 evaluation、基准入口、docs、tests 和 .git；既有 swebench 权限模块是生产导入依赖，保留它不提供评分材料。没有挂载 Docker socket 或宿主根目录。子进程收到环境白名单，宿主开启的可继承哨兵描述符经 close_fds 清除。

| 接受的探针 | 真实结果 | 假上游物理请求 | 证据 |
|---|---|---:|---|
| 正向 | read_file → edit_file → Bash 探针 → pytest → Judge → completed，退出 0 | 4 主 + 1 辅助 | [请求](positive/fake-requests.json)、[运行时](positive/runtime.jsonl)、[预留/结算](positive/ledger.jsonl)、[启动](positive/launch.json) |
| 取消 | 工具父进程和 Python 后代已开始写入；原生异步取消后 run_end=cancelled，退出 0 | 4 | [运行时](cancel/runtime.jsonl)、[账目](cancel/ledger.jsonl)、[回执](cancel/receipt.json) |
| 总期限 | 6 秒期限达到，宿主撤销准入并杀容器，退出 137；没有伪造 RunResult | 12 | [账目](timeout/ledger.jsonl)、[回执](timeout/receipt.json)、[容器事实](timeout/container-inspect.json) |

期限探针的清理时间包含在回执 elapsed 中，因此其墙钟记录约 7.2 秒。正常/取消探针的独立试验期限为 30 秒，均不冒充冻结自主实验的 900 秒配置。

正向 Python 子进程真实检查了工作区写入、绝对/父目录/符号链接越界读写、评分和其他候选、宿主 HOME/凭据哨兵、完整证据目录、/proc 根与继承 FD、运行包只读、无管理 socket、拒绝宿主 loopback TCP 与 TEST-NET 受控地址、必要 Unix 通道可用。所有检查为 true，哨兵字节未变。取消和期限结束后文件哈希及上游请求数稳定，Docker 状态已停止并移除；不宣称抵御全部内核漏洞。

模型服务只记录请求和返回协议响应，检查实际收到的工具证据。它没有读写任务文件或执行 pytest。辅助请求实际 max_tokens=1024，主请求 64000，没有为过额度静默降低参数。pytest 为真实进程，完成依据来自当前 generation 的实际通过与受控 Judge；受控 accept 不证明自主语义判断能力。

## 预算控制与回归

socket 的单元/run/角色由宿主绑定，忽略客户端归属头；固定模型/上游地址，不能转发任意 URL、读宿主文件、执行命令或重置预算。宿主先持锁预留次数、完整出站输入和最大输出，再持久化 attempt_id 后才启动上游。并发中的两个 64000 不能越过输出总量 100000。每条 socket 最多 8 个处理线程，请求正文和响应帧均有 2 MB 上限。

SDK 重试设为 0；既有 Gateway 重试保留，每次实际转发独立准入。响应丢失/断流保留 ambiguous，拒绝自动重送。usage 缺失、非法、错误响应或超时保留预留，实际 usage=null；SSE 使用声明为累计的最终 usage，不叠加重复片段。缓存包含在 prompt，reasoning 包含在 completion。后两项仅核验了本地协议契约，真实 DeepSeek 输出/thinking/cache/usage 定义仍须在计数缺项中核验。

测试覆盖 N+1、最后额度竞争、跨角色/跨 run 伪造、重复报文、SDK/Gateway 错误重试及丢失、64000 并发预留、工具 schema 超限、未知计数、总输入不足、SSE、断流、缺失/非法 usage、请求超时、取消、排队过期、禁止目标/模型/路由、账目写入失败及重启拒绝。每次尝试由宿主记录，不把逻辑轮次或 SSE chunk 算成请求。账本以 O_EXCL 建立，已有或部分写入账本拒绝重开；没有复杂恢复服务。

[related-tests.txt](related-tests.txt)：32 passed、1 skipped；跳过 `test_real_paired_entrypoints`，原因是该 InfCodeX 双侧测试要求显式 opt-in、本地固定构建、Node LTS 与 Linux libseccomp，本轮未启用。没有重跑全部 Core 或历史 compaction 专项，也没有把已经通过的历史测试列为当前失败。[静态检查](static-check-final.txt) 通过。

初始失败不是删除掉的：首次辅助请求因实验 ContextVar 未传入 Gateway 工作线程而走错 socket，真实 max_turns；修正同线程请求构造的通道选择后完成。随后取消适配错误地以 RunOptions 中保存的 Event 代替实际异步取消，触发宿主期限；改为原生 task.cancel 后返回了真正 cancelled，验收脚本又因期待抛异常而失败，随后纠正该断言。各次原始日志、请求与哈希见 [provenance.json](provenance.json) 和 attempts。只修实验接入，不修改生产取消或完成语义。

## 复用、缺项和停止范围

[commands.md](commands.md) 给出准备、正式隔离探针、核验、状态及默认拒绝的在线入口。Linux 迁移需要获准使用现有 Docker daemon、该非特权运行选项、固定 digest 基础镜像、锁定 cp312 Linux wheels、两个源码提交及冻结材料。只携带裁剪运行包和当前任务初态；不复制私人 HOME、密钥、另一候选、评分或 Git 对象库到实例。每个单元重新建立私有会话/索引，核对哈希。

后续解锁需要核验所选 DeepSeek 最终出站输入的可信计数/保守上界（含 schema、消息和封装），以及最大输出对 thinking 的约束与可靠 usage；并取得明确覆盖本轮账号、模型、范围和辅助请求的费用授权。否则 online 保持拒绝。不能将本地字节计数器换上去声称已具备真实 token 硬限额。客户端断开也不保证供应商停止计费，本轮不承诺金额上限。

公开记录是原件的脱敏派生副本，私有宿主路径替换为占位符，Docker 记录选取实际相关字段，原件哈希和派生哈希一一对应。原件留在受控 artifact 目录，没有覆盖上阶段 manifest；镜像、wheels、私有 HOME 与二进制不提交。未运行 Windows、正式付费审查、自主任务或真实模型效果复测。本轮到隔离与本地限额落地、真实计数缺项明确为止。
