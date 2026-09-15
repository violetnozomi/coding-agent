# 真实 SA / NZ 生产入口离线回放

本轮两侧真实入口均完成现有 F 任务：读取 → pytest 实际失败 → 原生局部编辑 → 同命令通过 → 正常终止。冻结文件的独立复测及额外功能验收均通过。受控 Provider 只返回协议响应，不读写任务工作区、不执行测试；这证明信息和执行链路，不证明自主编码成功率。

执行版本：NZ-Coder **`2c955bd7680808dc69ad560f35e1d5e77e5f5ba2`**（审查基线 `2bf96b58980b330126425265b3e10f289f4954d5`，开始时 HEAD 相同、工作树干净）；InfCodeX **`d3a812379b589597347f5be12d5b68477e577f02`**，参考源码未修改。本文的证据提交不改变执行代码。

| 请求配置 | 实际配置 / 路径 | 本轮证据范围 |
|---|---|---|
| InfCodeX `--agent-mode sa --mode json --reasoning off` | CLI → embedded Runtime → `runManagedTask` → `dispatchManagedTask` SA 分支 → `runKodaX` → 默认 coding preset → `runSubstrate` | 真实 CLI、SA 循环与文件/Bash 工具；不代表 AMA、managed specialist 或 Sidecar 能力 |
| 初次 `--no-session --auto` | `createKodaXOptions` 未传 auto；新 Runtime session 的 permissionMode 未设置，权限 Promise 未解决，进程 exit=0，无 tool.result / run.result | 实测阻断；不能当作 read 已执行或任务完成 |
| 最终 `--session offline-F` | 官方 `createKodaXRuntime().sessions.create/updateSettings` 预设 `auto-in-project`、`rules`、SA，再启动原 CLI | 属于显式隔离会话配置；不是默认临时 CLI 已修好，也没有修改参考核心 |
| NZ 未设置 retrieval strategy / allowed_tools | 与 headless 入口相同的 `prompt.build`、`build_product_run_environment` → `NativeSDKRunner` → `AgentRunner` / `ProductionToolRuntime` / `ProductionCompletionVerifier`；override=null，运行解析为 policy | 真实生产 Runner；不声称跑了 NZ CLI。旧显式 guidance 消融配置保留 |
| 模型边界 | InfCodeX 官方 custom OpenAI Provider、stream=true；NZ 真正 OpenAICompatibleProvider + OpenAI client、stream=false；实际均未发送 reasoning_effort | 本地脚本模型；不声称两侧线上推理预算相同 |

源码定位：参考 `src/kodax_cli.ts:440` Runtime 桥接、`:3582` 单任务入口，`src/cli_option_helpers.ts:377`，`src/sdk-runtime.ts:5571` 权限桥接与 `:6784` 策略，`packages/coding/src/task-engine.ts:177` SA 分派，`packages/coding/src/agent.ts:60` 默认 Runner。NZ 生产配置见 `interface/headless.py:237`、`runtime/execution/native_sdk.py:66`。行号以固定提交为准。

环境与费用隔离：

- 使用本机已有 Node **24.18.1**：`/home/pyh/.local/lib/python3.13/site-packages/playwright/driver/node`；未下载或替换系统 Node。SHA256 `f3432a45b03b2da0d270095fdd8813dc34cbea73f5fc8b18c7a384b7cf9b333a`。构建、CLI、其子进程的 PATH 首位均是指向此文件的 node。
- 锁文件 `package-lock.json` SHA256 `d891cf1247835a2d705a6b93627295b326edbc92bd33dfe2f835614fb1213ed9`。离线 npm ci 因缓存缺失失败；随后取消单命令代理、使用锁文件原有 registry/integrity 下载依赖，exit=0。未运行安装脚本、sudo、npx 下载；未修改全局 shell 配置。
- `npm run build:packages` 成功；再运行 `node node_modules/typescript/bin/tsc -b tsconfig.build.json --force` 成功，最终 smoke 使用这些构建产物。源码、锁文件和实际产物哈希见 [manifest](evidence/sa-smoke-2026-09-15/environment-manifest.json)。
- 当前环境拒绝 unshare/bwrap 网络 namespace，因此正式执行使用 **HTTP over Unix socket**，而非 TCP loopback。Node preload 仅把 Provider fetch 传输到 Unix socket；NZ 使用 httpx UDS transport。Linux seccomp 在 exec 前阻断所有非 AF_UNIX socket/socketpair 和 io_uring，继承到文件工具启动的测试进程；专门回归证明 exec 后及孙进程均无法创建 IPv4/IPv6 socket。
- HOME、KODAX_HOME、会话、工作区独立；环境变量白名单，不继承真实 API 密钥、代理、在线 fallback 或私人 MCP/记忆配置。仅本地假密钥。正式 smoke 的父测试进程也处于网络过滤中。依赖准备的网络下载与模型请求分开记录。

本轮修正的事件口径：

| 原始事实 | 修正后的处理 |
|---|---|
| 真实 `createJsonEvents.onToolResult` 只有 type/id/name/content 和活动信息，没有 status/exit_code/returncode | 保留 content；status=unknown、executed/command_failed/dispatch_failed=null，reason=missing_execution_status |
| tool.start 没有结果；非法退出状态；未执行、拒绝、取消、超时 | 保留缺失原因及不同状态，不伪造成功，不按正文的 failed/error 判错 |
| 缺 run.result，非法 success，或进程非零/超时 | 不标 completed；终止事件不能覆盖非零/超时 |
| iteration.end / turn.completed | 分别保留快照/生命周期；都不直接变成一次 llm_response。参考 run.result 实际不带累计 usage，快照可能重发，因此轨迹总 tokens=null |
| 独立验收通过但历史工具事实 unknown | 验收通过单独保留；历史恢复仍为 unknown，评分和重评分不能洗成已证明恢复。不可观测调用/token/cost 不填效率零值 |

真实 emitter 夹具由 `tests/evaluation/fixtures/infcodex-emitter.mjs` 直接调用固定源码产生。另保留实际 CLI 正常及不完整 JSONL，普通单元回归使用真实形状。参考 usage 结构核对了 `packages/llm/src/types.ts`、`packages/coding/src/agent-runtime/event-emitter.ts` 和 `src/kodax_cli.ts:1840` 的真实生产路径；没有假设终止事件累计用量。

最终固定版本回放结果：

| F 任务 | InfCodeX SA CLI | NZ 生产 Runner |
|---|---|---|
| 初态 | 两文件 SHA256 与另一侧相同 | 两文件 SHA256 与另一侧相同 |
| 读取 | read calc/service.py | read_file calc/service.py |
| 首次真实测试 | `python -m pytest -q`；test_empty_count / ZeroDivisionError，calc/service.py:2 | 同命令同失败；工具 executed=true、command_failed=true、dispatch_failed=false |
| 下一次模型输入 | 含上述真实输出 | 含上述真实输出；不是只收到 trace 的失败摘要 |
| 局部修改 | edit 的 old_string/new_string | edit_file 的 old_text/new_text |
| 修复内容 | `return 0 if count == 0 else total / count` | 相同；测试文件哈希未变 |
| 复测 / 终止 | 同命令 1 passed；真实 run.result success=true | 同命令 1 passed；VerificationManager 失败→通过；run_end completed，mutation_generation=1 |
| 独立验收 | 冻结文件 pytest exit=0；零分母、正常比例、负数分子断言 exit=0 | 相同 |
| 实际请求 | 本地 HTTP 收到 5 次；无辅助请求 | 本地 HTTP 收到 5 次；runtime purpose=coding:5 |
| 工具历史状态 | 四个工具结果仍 unknown；命令正文与最终验收独立保留 | ok → nonzero → ok → ok；四次 executed=true |

原始证据：[参考 stdout](evidence/sa-smoke-2026-09-15/infcodex/stdout.jsonl)、[参考模型请求](evidence/sa-smoke-2026-09-15/infcodex/requests.jsonl)、[参考归一化](evidence/sa-smoke-2026-09-15/infcodex/normalized.json)；[NZ Runtime trace](evidence/sa-smoke-2026-09-15/nzcoder/runtime.jsonl)、[NZ 模型请求](evidence/sa-smoke-2026-09-15/nzcoder/requests.jsonl)、[NZ 归一化](evidence/sa-smoke-2026-09-15/nzcoder/normalized.json)。对应目录包含命令/环境、原始 stderr、协议响应、diff、冻结文件和 acceptance.json。外部验收在 Agent 终止后运行，其额外断言未进入模型请求。

所有调试调用也计入 [manifest](evidence/sa-smoke-2026-09-15/environment-manifest.json)：trial1=1/0，trial2=5/0，trial3=5/8，trial4=5/5，final=5/5，pinned=5/5（参考/NZ），**合计 49 次本地 HTTP 请求**，包含失败响应；没有收费模型调用。trial2 的 NZ 初始化参数遗漏在发请求前失败；trial3 的 NZ 未提供测试许可回调，命令被拒绝，受控服务拒绝继续伪造成功，8 轮有界结束。最终仅为 `python -m pytest -q` 提供已有授权范围内的许可回调；参考权限通过官方会话配置设置。各轮原始请求/响应、会话和产物完整保存在 `/home/pyh/.codex/artifacts/nz-sa-smoke-20260915`，调试 stdout/stderr 亦随文保留。服务每个成功响应的 100/20 tokens 是测试值，不能作线上效率证据。

实际命令与验证：

```bash
# 参考目录；同一 Node PATH；直连取消代理；锁定依赖
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  PATH=/tmp/nz-sa-smoke-20260915/bin:/usr/bin:/bin npm_config_userconfig=/dev/null \
  npm ci --ignore-scripts --no-audit --no-fund --fetch-retries=0
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  PATH=/tmp/nz-sa-smoke-20260915/bin:/usr/bin:/bin npm_config_userconfig=/dev/null npm run build:packages
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  PATH=/tmp/nz-sa-smoke-20260915/bin:/usr/bin:/bin node node_modules/typescript/bin/tsc -b tsconfig.build.json --force

# 项目根目录，显式受控真实入口；新输出目录不会覆盖旧试跑
NZ_RUN_PAIRED_SMOKE=1 \
NZ_REFERENCE_NODE=/home/pyh/.local/lib/python3.13/site-packages/playwright/driver/node \
NZ_SMOKE_OUTPUT_DIR=/tmp/nz-sa-smoke-20260915/pinned \
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q \
  tests/evaluation/test_paired_runtime_smoke.py --tb=short

python tests/evaluation/fixtures/offline_exec.py python -m pytest -q \
  tests/evaluation tests/runtime/test_native_product_runtime.py tests/runtime/test_native_runner.py \
  tests/test_skill_governance.py tests/test_edit_recovery.py \
  tests/security/test_artifact_boundary.py tests/security/test_workspace_file_access.py --tb=short
```

结果：[显式真实入口 2 passed](evidence/sa-smoke-2026-09-15/pinned-smoke.txt)；[相关回归 189 passed, 1 skipped](evidence/sa-smoke-2026-09-15/final-regressions.txt)。skip 原因是常规测试默认不启用需要本地 Node/固定参考构建/Linux seccomp 的双侧 smoke，该项已经以上方显式命令实际通过。Ruff 与代码/说明文档的 git diff --check 通过；原始证据中的 pytest 尾空格和 npm 末尾空行保留，整批证据的 diff --check 因这些原始空白返回 2，未清洗日志。没有运行 Windows、非 Python 任务、全仓测试或真实模型任务。

失败到通过证据：[基线三个状态反例均失败](evidence/sa-smoke-2026-09-15/status-before.txt)，当前回归通过。两项历史失败分别在 `git archive 2bf96b5` 的独立目录和当前代码运行，未回退主工作树、未修改原断言：

- `tests/test_loop_fake.py::test_context_overflow_stops_after_three_compaction_attempts`：实际 `[False, False, False]`，预期 `[True, True, True]`。
- `tests/test_loop_fake.py::test_pre_send_and_reactive_compactions_share_one_three_attempt_owner`：实际 `[False, False, False]`，预期 `[False, True, False]`。

见 [基线输出](evidence/sa-smoke-2026-09-15/baseline-compaction.txt) 和 [当前输出](evidence/sa-smoke-2026-09-15/current-compaction.txt)。没有把全仓说成通过，也没有重开预算专项。

结论分层：适配器单元回归证明判定；真实 emitter 证明字段形状；双侧真实运行时受控执行证明请求、工具、修改和验收链路；真实模型效果未验证。已登记的参考临时 CLI 权限配置断点与原始工具状态缺失不等于 Core 编码能力缺失，本轮未扩展修复。另两次独立子智能体审查启动均因模型容量错误失败，没有获得其审查意见，不能宣称独立审查通过。
