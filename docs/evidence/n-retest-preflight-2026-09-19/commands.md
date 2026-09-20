# 命令清单与执行边界

本轮已执行（无模型网络请求）：

```bash
git status --porcelain=v1
git rev-parse HEAD
git rev-list --left-right --count HEAD...origin/main
git ls-remote origin refs/heads/main

env PATH=/tmp/nz-paid-comparison-20260916/N/bin:/home/pyh/miniconda3/bin:/usr/bin:/bin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  /home/pyh/miniconda3/bin/python tests/evaluation/fixtures/offline_exec.py \
  /home/pyh/miniconda3/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=/tmp/nz-n-preflight-0885c87-20260919/pytest \
  tests/runtime/test_node_verification_runtime.py \
  tests/runtime/test_verification_contract.py \
  tests/runtime/test_completion_gate.py tests/test_verification.py \
  tests/test_verification_planner.py tests/test_runtime_state.py
```

初态 Node subprocess 的完整 argv/cwd/stdout/stderr/exit 见 initial-node-test.json。七文件用 shutil.copytree 原字节重建；逐文件 SHA-256 与 historical initial-hashes 比较，未修改初态。基线及所有 pytest 均通过 offline_exec.py 继承网络隔离。

补充 smoke 的复现步骤（仅离线；不是付费命令）：

1. 在新的临时目录写入已有 `node_project` 的原始 index.js；只在这个负例目录分别放 `const {describe}=require('node:test');describe('empty',()=>{});`（真正零测试），或 `const test=require('node:test');test.skip('skipped',()=>{});`（全 skipped）。另保留空文件样本；Node 将其计作一个文件级测试，不能视为 tests=0。
2. 通过上述隔离器真实执行 `node --test literal.test.cjs`，保存 exit/stdout/stderr。
3. 复用 `tests/runtime/test_node_verification_runtime.py` 的 `_run`，在 `pytest.MonkeyPatch.context()` 下执行 `[READ, EDIT, TEST]`、limit=3；使用其默认任务、受控 Provider 和受控 semantic verdict，不接入远端。
4. 保存 state.verification_contract、mutation/verification generation、verification_result、terminal_boundary_settled、主/辅助替身响应数量。观察值见 supplementary-smoke.json，不把其 completed 当成正确结果。

后续获授权后的命令形状（本轮未执行，**不是就绪的启动命令**）：

```text
cwd = NEW_N_WORKSPACE（重建后再次匹配七文件 initial hashes）
env = 历史 command.json 的环境布局，HOME/KODAX_HOME/SMOKE_OUTPUT/SMOKE_SOCKET
      由独立进程环境字典映射到新目录；TASK_PROMPT 原字节不变
argv = [历史 Python, 仓库/tests/evaluation/fixtures/offline_exec.py,
        历史 Python, 已审核的 NZ 单侧入口]
```

先确认本轮付费授权同时覆盖最多 12 实际主请求和生产自然触发的辅助请求；再核对单侧采集、实际请求计数、日志 reasoning_content 删除和逐事件状态快照。不得直接运行 `/tmp/paid_pair_compare.py N ...`，因为其 main 会重跑 InfCodeX。不得照搬旧代理的 pooled 16-request cap 作为本轮 12 主请求上限。

完成后冻结工作区；对冻结副本执行原 `node --test literal.test.cjs` 和原 acceptance.json 中 independent.argv（node -e 仅用于外部独立验收，不能当 Runtime 声明测试通过），保存最终 diff、所有源码 hash 和 A–F 原始引用。无自动重试或第二个真实样本。
