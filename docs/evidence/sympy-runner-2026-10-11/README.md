# SymPy 原生测试入口：离线接通

本轮接通原生测试准入、解释器绑定和生产链的工具证据传递，但完整 SWE CLI 的正常退出仍有关闭阶段超时，按**出口 B**保留这一具体阻断；同时缺少本轮费用授权。**自主解题 not_run，官方 Agent 补丁评分 not_run**。本轮提示明确不构成费用授权，历史授权已结束，没有创建新费用授权回执。金补丁自检不是本次 Agent 成绩。

起始 HEAD `c329b9211fc9e50e2e33be8c41afd6e2656e766e`，工作树干净；前置代码 `565223d720fd1f849d7eae45e5a5710724b17dfb`。运行时使用起始提交加本轮局部修改，精确源码摘要及交付提交见 [execution.json](execution.json)。没有回退分支，没有修改 F1–F5、评分器、Agent 循环或完成门。

## 缺口与修正

| 真实缺口 | 最小衔接 | 验收 |
|---|---|---|
| strict 仅接受 runtests.py；bin/test 被拒绝 | 复用 native_runner_positional_selectors，按初态 bin/test 的参数语义识别窄目标 | 原实现两项真实 Bash 回归失败；当前 test_sympify 和相对测试文件可执行 |
| bin/test 不进入验证分类/精确测试识别 | 扩展已有 targeted 分类和精确测试判断 | 原实现三项分类回归失败；真实 VerificationManager 记录 passed/failed 和本次命令 |
| 新 runner 接入后，真实零选择输出 exit=0 被当成 passed | 在已有零测试检测中识别 `tests finished: 0 passed, in ...` | 当前 skipped，修改后的验证义务仍保留；不误判正常 skipped/expected failures |
| Bash 登录 shell 重置启动 PATH | 独立镜像中的可信 profile 固定项目 PATH；Agent 仍用绝对解释器启动 | 实际 Bash 子进程 Python 3.9.20，Agent Python 3.12.13 |
| 工作区新建且未信任，无人值守审批不成立 | 启动夹具通过既有 permission_asker 批准工作区内准入合格的窄验证 | 实际审批回执保留；未修改 PermissionManager，拒绝规则仍优先 |

允许形式是 `python3 bin/test test_sympify --no-colors`，或 `python3 bin/test sympy/.../tests/test_name.py`。支持初态 runner 中经校验的输出/关键字/种子/超时等参数；不开放任意脚本、绝对解释器、越界路径、未知选项、交互 pdb 或无限 rerun。runner 本身必须是当前 workdir 内的工作区文件，越界 symlink 被拒绝。没有按实例 ID 或隐藏测试名称写生产特判，没有自动改测试选择。

Bash 保留请求/实际命令、cwd、退出码、超时/取消和输出。新增 resolved_executable 是**启动 PATH 的解析结果**，不单独证明进程版本；本次还核对实际 runner 输出的 executable/3.9.20 与只读环境探针。成功文本、测试状态和任务正确性分开。

## 固定环境与真实入口

数据集仍为 `princeton-nlp/SWE-bench_Verified`，revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`，test；SymPy 初态 `c4e836cdf73fc6aa7bab6a86719a0f08861ffb1d`。公开初态的 README 和 bin/test 支持此原生 runner；没有用 FAIL_TO_PASS/PASS_TO_PASS 选健康检查。

复用已存在的 `nz-swe-sympy-22914:20261010`，新增仅含 PATH/profile 的 [Dockerfile](environment/Dockerfile)。没有下载依赖、升级 harness 或复制 /testbed、未来 Git、金补丁、评分脚本。Agent `/usr/local/bin/python` 3.12.13；项目 `/opt/miniconda3/envs/testbed/bin/python3` 3.9.20、mpmath 1.3.0、无 pytest。真实工作区导入定位是 `/workspace/runs/sympy__sympy-22914/sympy/__init__.py`，bin/get_sympy.py 的 path_hack 从本工作区加载；文件无 Agent 修改。

入口复用 `scripts/review_effects_execution.py:launch_argv` → 现有 SWE CLI → spawn → ProductRunEnvironment/AgentRunner → ToolExecutor → Bash → 真实 bin/test。只读代码包不含 NZ evidence/.git；输入只含四个公开字段；fresh HOME/Session；非 root、network=none、cap-drop、read-only root、仅本次私有目录挂载。所有模型传输走 Unix socket 和回环受控服务，SDK retry=0，无真实密钥、代理或在线 fallback。审批是启动配置，不是模拟工具执行。

## 三条离线证据链

1. [健康检查请求](health/provider-requests.jsonl) #1：原生 read_file 读取 bin/get_sympy.py；#2：Bash 实际执行 `python3 bin/test test_sympify --no-colors`；#3：真实请求包含本次输出 **45 passed、5 skipped、2 expected to fail** 及 Python 3.9.20。VerificationManager 记录 targeted/passed；没有改文件、没有解决题目。
2. [负向请求](negative/provider-requests.jsonl) #1：读取独立微型项目 bin/test；#2：真实 Python 进程输出带 passed 字样的普通文字后 exit=1；#3：模型收到 `Command exited with code 1` 和真实断言诊断。工具 executed=true、dispatch_failed=false、command_failed=true；VerificationManager 记录 targeted/failed，没有把 stdout 的 passed 当成功。正式 SymPy 初态未改。

3. 固定 SymPy 初态真实执行不存在的公开选择器，exit=0 且输出 `tests finished: 0 passed`，见 [zero-selection.txt](zero-selection.txt)。独立零测试微型进程又穿过同一真实 CLI，结果抵达后续模型请求并记录 skipped。这不是自主解题。

原生终答明确写“只做健康检查，未修复问题”。三条 Core 受控回合均记录 run_end=completed；SymPy 外层的关闭阶段 timeout 独立保留。标准预测为空，official_resolved=null，未执行官方评分。不得把 completed、健康检查通过或受控回答当作自主 solved。最终三例各 3 个本地主请求、0 个辅助请求，共 9 个；所有均为受控回放，不收费。主/辅助实际请求、归档和事件位置见 [execution.json](execution.json)；请求 token 是本地测试值，不能用于线上效率或价格比较。

[真实工具事实](health/execution-facts.jsonl)、[运行时](health/runtime.jsonl)、[审批](health/permissions.jsonl)、[环境](health/environment.json)、[Session/Artifact 索引](health/evidence-index.json)、[负向工具事实](negative/execution-facts.jsonl)、[零测试链](empty/execution-facts.jsonl)。完整 owned Session/事务快照保留在私有原件；短工具证据在原始请求及工具事实中完整可读。TraceRecorder 是投影，不冒充无损 HTTP。原件/公开副本哈希见 [evidence-index.json](evidence-index.json)。

## 验证、资源与停止

可重用的离线命令（必须先有上述本地镜像和干净公开初态，不会自动下载）：

```bash
NZ_SWE_SYMPY_SMOKE=1 \
NZ_SWE_SYMPY_BASE=/path/to/public-base \
python -m pytest -q -rs tests/evaluation/test_swebench_native_runner.py \
  --basetemp=/tmp/nzsympy-smoke
```

这里的 3 个 Agent 轮次、主/辅助合计最多 6 个本地协议请求、Agent 60s、Bash 30s，都是离线夹具边界，**不是未来付费 SWE 上限**。外层 180s 包含现有事务快照归档，归档磁盘配额 0.1 GiB。实际命令、结果及跳过条件见 [test-results.txt](test-results.txt)。当前 284 项相关回归、29 项 Bash/环境边界回归和 3 项真实容器链路通过；ruff、compileall、git diff --check 通过。3 项容器回归验证通道与状态真实性，不证明 SymPy 整体正常退出。

本轮新出现的夹具失败没有归入历史：首次缺审批而未执行；负向替身最初把读取的脚本文字误当结果；“health 不能 completed”的断言混淆了运行结束和题目解决，已换为独立 patch/official 和失败状态检查。SymPy 事务快照约 40 MiB，20 MiB 归档配额不足；80s 外层等待结束时仍在归档。诊断后仅调整夹具归档配额/等待，生产测试超时、模型轮数、权限和完成逻辑没有放宽。一次夹具检查还误用了未公开的计数字段，已中止并改为真实 run_end.last_verification。此前失败、部分归档及原件都保留。

未运行 Windows、全仓测试、收费模型或官方 Agent 补丁评分；原金补丁自检保持不变，不重复计分。Astropy 仍 environment_blocked，正式十例仍 not_run，ID/分母不变。本轮没有重跑或改动历史 compaction 测试，不能据此宣称全仓通过。

**一次性授权缺口：** 本轮金额/币种，主辅合计物理请求/token 预留/期限，主辅实际模型及 thinking/reasoning/stream/输出配置、账号配置别名确认，以及是否接受现有经验输入计数的剩余风险。实例范围已明确为 SymPy 一次自主校准，不需要重新贴密钥。既有源码默认 500/200 轮未用于本次 smoke，也未被当成在线授权上限。

唯一后续候选是关闭阶段等待后台仓库构建：`RepoIntelligenceService.close()` 调用 executor.shutdown(wait=True)，SWE worker 在返回结果前执行环境关闭。本次 run_end 已出现，外层仍在 60s 处超时；目前源码和时点支持这一候选，但未通过线程栈确认具体等待归属；不能宣称它是唯一充分原因，也不能归因于测试失败、模型多跑或完成门。本轮不修改该机制、不增大 Agent 上限，完整正常退出尚未稳定证明。当前只能证明原生测试信息流和控制流接通，不能证明解题能力提高。

## 工程案例口述（约 90 秒，离线接入案例）

我维护的是一个终端 Coding Agent。这次希望用一个真实 SWE-bench SymPy 项目检验它的测试执行能力，但没有本轮模型费用授权，所以我没有把受控回放包装成自主解题。首先，我发现 Agent 环境有 Python 3.12，而这个老项目使用 Python 3.9，且没有 pytest，实际测试入口是仓库的 bin/test。原有准入和验证分类都不支持它。我先建立失败回归，再复用已有原生 runner 机制接通窄测试，保留路径、权限和执行时限。第二个问题是 Bash 登录 shell 会重置 PATH，所以只传环境变量并不能保证解释器正确。我通过隔离镜像的可信配置绑定项目 Python，同时用绝对路径启动 Agent。最终真实测试子进程运行了公开健康检查，结果和版本信息进入了下一次模型请求。负向进程退出 1 被保留为失败，零测试退出 0 则被识别为 skipped。我的贡献是接通并验证证据链，而不是证明解题成功：Core 已结束，但外层关闭仍出现超时，自主修复和官方 Agent 补丁评分尚未运行。这些限制都和原始请求、工具事实及归档对应保存。
