# 三组自主任务对照：授权前干跑

状态：**`not_run: missing_model_authorization`**。任务链未给出本轮真实 Provider、endpoint、模型版本、实际推理档位或总费用预算。历史本地 `local-smoke` 与固定 100/20 token 不是有效模型授权；未读取或打印 API 凭据，未启动 Agent、脚本 Provider 或模型转发服务。本轮模型请求为 0。各任务的真实用量、费用、耗时、最终验收和过程证明均为 null，不能填零代表效率。

开始时 NZ HEAD=`b54b3f52d897f41ef417bd67e0626b24e74d91a0`，无后续增量、工作树干净；运行时仍是上轮 `2c955bd7680808dc69ad560f35e1d5e77e5f5ba2` 的实现。InfCodeX HEAD=`d3a812379b589597347f5be12d5b68477e577f02`、参考工作树干净。根目录及父目录没有实际 AGENTS.md，遵循会话提供的仓库指引。本轮没有生产代码或既有测试改动。

已读取[上轮报告](real-sa-smoke-2026-09-15.md)、其原始 Runtime 记录、事件和构建 manifest。已有 Node 24.18.1 的可执行文件哈希、锁文件和记录的所有源码/构建产物哈希均匹配；离线 `InfCodeXReferenceAdapter.probe()` 可用。未重新安装、构建、联网探测或下载依赖。

运行清单：[run-manifest.json](evidence/autonomous-comparison-2026-09-15/run-manifest.json)。包含恰好六个 run、repetitions=1、独立会话名和工作区、两侧相同的输入哈希、真实入口配置模板。所有 execution_ready=false；未知 Provider/effort/费用/token/调用上限保持 null，不能直接执行付费流程。只执行了离线准备和初始项目测试：

| 任务 | 固定初始项目与可见需求 | 本轮夹具干跑 | InfCodeX SA | NZ 生产 Runner |
|---|---|---|---|---|
| F | 复用 `_fixture_f`；空分母返回零，保留正常比例，验证并报告 | 真实 pytest exit=1；test_empty_count / ZeroDivisionError | not_run: missing_model_authorization | not_run: missing_model_authorization |
| B | 复用 `_fixture_b`；公开 API 改名 render_product，迁移真实调用方，兼容别名可保留 | 真实 pytest exit=0，2 passed；只证明旧测试可运行，未满足新接口需求 | not_run: missing_model_authorization | not_run: missing_model_authorization |
| N | 本机已安装 `escape-string-regexp@2.0.0`，原始 MIT 源码/声明/说明/package.json 原样复制；增加公开 node:test，要求转义结果支持 Unicode RegExp，保留匹配与入参拒绝行为 | Node 内置测试真实执行，2 passed、1 failed；`/^a\-b$/u` 抛 SyntaxError | not_run: missing_model_authorization | not_run: missing_model_authorization |

N 是现有无运行时依赖的小模块，来源文件哈希已记录；新增的是公开任务测试，不是新增评测框架。没有执行其 npm 安装或开发测试脚本。上述失败是任务初态，不是 NZ/InfCodeX 的失败轨迹；没有 Agent patch、最终文件、隐藏验收结果或自主恢复证据。

F/B 保留原始输入源码和测试，但清单不应用历史 `must_change`、`forbidden_text`、`requires_verification_recovery` 评分条件。F 不要求先失败；B 按新接口与调用方公共行为验收，不要求改动固定文件清单或删除兼容别名。N 的源码没有预填修复。三个模型需求均不限定首个工具、读取位置或工具调用顺序。

授权后的执行约定已冻结在清单中，当前不执行：

- InfCodeX 使用官方 `sessions.create/updateSettings` 建立隔离的 **preconfigured-session**，SA + 项目内权限 + rules；随后真实 CLI `--mode json --agent-mode sa --session <新会话>`。不调用仍使用 `--no-session --auto` 的通用 adapter.run 旧分支；复用 probe/执行与归一化能力。现有 smoke 专用准备脚本固定 local-smoke/offline-F，不能原样充当真实模型配置。
- NZ 使用 `prompt.build` → `build_product_run_environment` → `NativeSDKRunner`，不显式覆盖 retrieval strategy、不套用 guidance 实验或固定工具列表。计划 stream=true 与参考侧一致；实际 Provider 支持及最终请求字段仍待所授权模型确认。
- `ControlledProtocol.respond()`、`native_scenario` 的确定性模型均不用于自主对照。smoke 的 unix-fetch 只是本地传输；现有代码没有已验证的授权远端转发/总费用控制。不能假设真实请求会自动穿过该服务，不能解除工具进程的网络过滤。
- 现有 smoke 的 seccomp 仅隔离网络，没有建立阻止 Bash/解释器读取历史答案、另一侧工作区和宿主私密目录的文件可见性边界。目录和 HOME 不同不足以证明访问隔离；真实运行前这项条件必须落实。当前记为通道/隔离未验证，未开展新代理平台或 Core 开发。
- 一旦真实执行，只允许三任务×两侧各一次，不失败后自动重跑。主请求、辅助请求、重试都计量；费用定价未知时不能宣称金额硬上限。当前没有擅自选定数值上限、推理档位、模型或预算。

最终验收口径：冻结 Agent 交付文件后，独立检查 F 的公共比例行为、B 新 API 及真实消费者行为、N 的字面量匹配与输入契约；具体隐藏断言未生成在任务目录中。用户要求的测试是否由 Agent 实际执行、恢复是否可观测、最终功能正确性分别报告。参考工具缺失状态继续 unknown/null；不能因此认定代码功能错误，也不能用独立通过补写历史工具成功。公开测试修改按需求允许，外部验收不依赖被修改测试作为唯一依据。

可重复执行的**离线**命令（输出目录必须是新目录，不覆盖已有初态）：

```bash
mkdir -p /tmp/nz-autonomous-dry-home-20260915
env -i HOME=/tmp/nz-autonomous-dry-home-20260915 \
  PATH=/home/pyh/miniconda3/bin:/usr/bin:/bin LANG=C.UTF-8 \
  PYTHONPATH=/home/pyh/nzcoder PYTHONDONTWRITEBYTECODE=1 \
  python tests/evaluation/fixtures/offline_exec.py python \
  docs/evidence/autonomous-comparison-2026-09-15/prepare-dry-run.py \
  /tmp/nz-autonomous-comparison-20260915-final
```

[实际输出](evidence/autonomous-comparison-2026-09-15/dry-run.txt)：准备脚本 exit=0；三个夹具分别 exit=1/0/1。初态测试在一次性副本中执行；六份候选工作区未运行测试、没有缓存或补丁，逐文件哈希两侧一致。说明中使用的干跑曾执行两次（第二次补齐入口模板），都没有模型/Agent 调用。Ruff 对准备脚本通过，manifest 的六条 not_run 记录、初态哈希、构建哈希和预期基线诊断已读回核对。没有重跑上一轮受控 smoke、全量测试或 Windows。

历史事项维持单列，不计入本轮新发现：独立子智能体审查上轮两次因容量错误未完成，本轮未重启专项。以下两项 compaction fake 断言的[基线证据](evidence/sa-smoke-2026-09-15/baseline-compaction.txt)和[上轮当前输出](evidence/sa-smoke-2026-09-15/current-compaction.txt)保留，本轮未修改或重跑：

- `tests/test_loop_fake.py::test_context_overflow_stops_after_three_compaction_attempts`
- `tests/test_loop_fake.py::test_pre_send_and_reactive_compactions_share_one_three_attempt_owner`

**尚不能提出新的 Core 修改。** 没有真实模型成对轨迹，本轮只能确认初始项目与配置清单，不能判定模型偶然选择或 Core 行为差异；不报告成功率、显著优势、耗时优势或对齐百分比。按授权条件在此停止。
