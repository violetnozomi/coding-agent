# SymPy 关闭阻断：真实取证、最小修复

**出口 B：关闭阻断已解决；一次自主校准因缺少本轮费用授权未运行。** 在线物理请求 0，Agent 自主补丁未生成，官方 Agent 评分 not_run，official_resolved=null。没有重跑金补丁自检、Astropy、正式十例或 InfCodeX 对照。

开工 HEAD / 审查基线 `faeacd86192d5f233afbc0110d7041c0d19b0534`，工作树干净，无后续增量。前置实现 `dee9f4381dc71ddea69ba1c1f286cf3828981b8d`；本次实现 `bf11b3468fdb49acacda25e80d8aaa146a9c637b`。固定 SymPy `sympy__sympy-22914`，Verified test revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`、base `c4e836cdf73fc6aa7bab6a86719a0f08861ffb1d`，harness 4.1.0。复用已有镜像和项目 Python 3.9.20；Agent Python 3.12.13。没有安装依赖、构建镜像或下载数据集。

## 已确认的根因

只进行一次带只读诊断的真实 SymPy 复现。原始[线程栈](before/thread-stacks.txt)在进入关闭后 45 秒捕获：主线程沿 `_agent_attempt_worker → _close_attempt_environment → ProductRunEnvironment.close → release_repo_intelligence → RepoIntelligenceService.close → ThreadPoolExecutor.shutdown → Thread.join` 等待；被等待的索引线程在 `PersistentCodeIndex.scan → _replace` 向 SQLite 批量写入 refs。run_end 已 completed，测试已通过，没有活跃 LSP、watcher、工具线程或 Queue feeder 阻塞栈。后者说明本次采样的实际归属，不能推广成所有关闭问题都来自索引。

| 实际边界 | 修复前 | 最终版本 |
|---|---|---|
| agent_execution_finished | completed，原生健康测试 passed | completed，同一测试 passed |
| cleanup_started | 是 | 是 |
| cleanup_finished | 未观察到 | 是，约 0.00198 秒，10 个资源阶段均完成 |
| worker_result_delivered | 无结果入父进程 | Queue.get 取得实际结果，父进程正常返回 |
| SWE 原生状态 / attempt 耗时 | timeout / 62.8 秒 | completed / 4.6 秒 |
| 自主补丁 / 官方评分 | 本地受控健康检查，无修复 / not_run | 本地受控健康检查，无修复 / not_run |

[修复前生命周期](before/lifecycle.jsonl)、[最终生命周期](after/lifecycle.jsonl)、[最终请求](after/provider-requests.jsonl)、[工具事实](after/execution-facts.jsonl)和[状态/源码哈希](execution.json)可复核。时间采用实际 monotonic / epoch；Queue.put 后的 enqueued 与父进程 get 后的 received 分开。原始超时回执不改写。前后均实际执行 `python3 bin/test test_sympify --no-colors`，45 passed、5 skipped、2 expected failures；受控 Provider 自述未修复题目，不能把运行结束当作 solved。

## 唯一生命周期修正

复用仓库服务已有停止 Event，将它传入现有索引：文件间检查取消，SQLite progress handler 中止正在执行的大批量操作，现有事务异常路径回滚。关闭先拒绝新提交并取消排队 Future，等待正在运行的所属任务与 watcher；正常路径仍 join 非守护线程。不响应停止的工作会在既有 5 秒范围内明确报 cleanup incomplete，由 SWE 已有进程树兜底处理，不伪装成成功。没有单独以 shutdown(wait=False) 结束正常清理，没有增加 Agent 60 秒 smoke 边界、默认轮数或新的服务/循环。

新增三个生命周期回归：冷扫描停止且无部分提交/排队查询不执行；真实 SQLite 长查询被中断；不响应取消的查询报错，排队构建被取消，任务释放后可重试关闭。最后一项额外暴露 CANCELLED Future 已从工作队列移除后不能等它再次被 worker 通知，已修正并保留[失败记录](tests/close-cancelled-retry-before.txt)。原始两个回归的[失败](tests/close-regression-before.txt)与[通过](tests/close-regression-after.txt)均保留。

最终真实 Runner/工具/测试进程回归断言：模型收到真实输出；run_end、各资源关闭、worker 入队、父进程接收顺序；没有遗留索引线程或活跃 worker 后代；结束后无新增模型请求；文件 diff 为空。失败和零测试对照仍分别 failed / skipped，不能洗成 passed。两项对照在首次修正版执行，最终取消重试细化后又运行了完整相关回归与真实 SymPy 健康链；各次运行源码摘要分开记录，没有冒充同一二进制。

## 命令与验证

```bash
python -m pytest -q -rs tests/test_repo_intelligence_service.py tests/test_repo_intelligence_closure.py tests/runtime/test_product_environment.py tests/security/test_environment_cleanup.py tests/evaluation/test_swebench_protocol.py
NZ_SWE_SYMPY_SMOKE=1 NZ_SWE_SYMPY_BASE=/path/to/prepared/public-base python -m pytest -q -rs tests/evaluation/test_swebench_native_runner.py -k sympy-health --basetemp=/tmp/nzc-f
```

最终相关组合 **59 passed、1 skipped**（[全文](tests/close-related-final.txt)）；skip 是 `test_real_cli_spawn_main_and_auxiliary_use_same_local_budget` 未启用其显式 Docker smoke 开关，不能称它已执行。最终真实 SymPy 链 **1 passed、2 deselected**（[全文](tests/close-runtime-final.txt)）；此前三种真实容器通道 **3 passed**（[全文](tests/close-runtime-after.txt)）。ruff、git diff --check 通过。没有 Windows 或全仓验证；未重跑/弱化两项历史 compaction fake，不宣称全仓通过。

本轮三个阶段共 15 个本地受控协议请求，辅助 0；与历史请求不混算。测试 usage 使用 JSON 字节计数，不是服务端 token 或账单，不证明模型自主恢复/编码能力。

私有原件在 `~/.codex/artifacts/nz-sympy-close-calibration-2026-10-11`，含完整 Provider 传输、owned Session/事务快照和执行事实；[索引](evidence-index.json)分别记录原件/脱敏副本 SHA。本次输出较短，Artifact ID 集合为空，证据直接位于实际模型请求；不存在可伪造的 Artifact 抽查结果。既有归档恢复行为由相关协议回归保留。归档初次错误地复制生成的索引缓存造成磁盘写满，已移除新建的不完整副本；随后只清理本轮已经停止的五个测试工作区派生索引缓存，回收约 1.17 GB，见[清理回执](derived-cache-cleanup.json)。原始请求、栈、工作区源码、diff 和 Session/Artifact 归档保留。当前宿主剩余空间约 1.8 GiB，正式启动前仍需核对实际磁盘；未删除其他工作区、历史证据或 Docker 对象。

## 一次自主运行入口及唯一授权缺口

[一次性启动脚本](launch-calibration.py)仅组合已有 Docker launcher、Unix 传输、RelayLedger、DeepSeekV41Counter、SWE CLI 和官方评分 CLI，不新增评测平台。非 root、network=none、只读代码、独立 HOME/Session/工作区，推理侧只有四个公开字段；隐藏数据仅供宿主 evaluator，正式评分使用新的 `nz-sympy-autonomous-20261011-official`，`--no-package`。所有主/辅助请求走同一宿主预算账本；SDK retry=0。不含预定工具动作/补丁/结束响应，不自动重试任务。仅安全冻结的非空或空补丁进入官方评分。

默认只核对配置，不启动 Docker/模型；未授权执行参数也[实际拒绝](calibration-refused.json)，exit=2，没有创建自主工作区或请求。原有账号在宿主项目 .env，不需要重新提供密钥；只有授权成立才读取。已核对的旧授权属于过期 `review-effects-formal` 范围，不能授权本例。当前实际默认是 500/200 轮，**本脚本拟固定主请求 24、辅助 6、总 30；单请求输出 8000，总输出 100000；输入经验计数每请求 100000、合计 500000；Agent 900 秒、单请求 180 秒**，不会动态加额度。thinking/reasoning_effort 未设置，stream=false；首个正常请求才能证明 Provider 的实际模型与 usage，未做付费探测。原 V4.1 计数是经验参考，超预留会止损，但不能撤销已出站费用，估算不是账单。

当前[准入配置](calibration-admission.json)固定实例/源码/计数契约和启动器哈希。获得一次性授权后，按实际当前 HEAD 生成对应授权回执再运行，不能把此检查时的 SHA 当作后续任意版本授权：

```bash
python docs/evidence/sympy-close-calibration-2026-10-11/launch-calibration.py
python docs/evidence/sympy-close-calibration-2026-10-11/launch-calibration.py --authorization ~/.codex/artifacts/nz-sympy-close-calibration-2026-10-11/authorization.json --execute
```

**可直接确认的最小授权：**“授权 project-env:DeepSeek 账号、deepseek-v4-flash 按上述固定请求/token/900 秒边界，对 sympy__sympy-22914 自主尝试一次，含全部辅助调用；人民币 10 元预算，到本次尝试结束失效。接受既有经验输入计数、超预留止损和在途/账单偏差的剩余风险。不换题、不重试到成功。”金额是待确认提议，**目前没有授权回执，没有任何在线调用**。

## 约 90 秒口述：生命周期工程案例

我维护一个终端 Coding Agent，希望用真实 SWE-bench 的 SymPy 项目校准它。前一阶段已经接通老项目的原生测试，实际运行了 Python 3.9 的 bin/test，测试结果也送到了模型。但 Core 明明结束，外层还是等到 60 秒超时。我没有直接调大时限或把后台线程设成不等待，而是在同一个隔离容器链路记录 Agent 结束、每个资源关闭和进程结果传递，并在临近超时时抓线程栈。证据显示主线程等线程池退出，索引线程仍在批量写 SQLite 引用。我的修改是把已有停止信号接到文件扫描和数据库进度回调，取消排队工作，等实际任务停止；不响应取消则明确报清理失败。回归同时检查了扫描事务回滚、真实数据库查询中断和取消后的关闭重试。最终同一个真实 SymPy 健康链正常返回，关闭约两毫秒，也没有遗留索引或工具后代。这个结果只证明生命周期修复。我没有拿到本轮新费用授权，所以还没有让模型自主生成补丁，也没有官方 Agent 成绩；已提供隔离、预算和评分入口，待授权后只执行一次，不追跑到成功。
