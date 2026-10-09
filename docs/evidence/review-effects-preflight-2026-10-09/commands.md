# 本轮执行命令与调用边界

以下路径用占位符脱敏：`REPO`为当前仓库，`LOCAL`为私有本轮artifact目录，`PYTHON`为本机Python3.13解释器。所有模型准备均无密钥、无在线代理、无MCP/私人记忆，`env -i`启动。

```bash
git rev-parse HEAD
git status --short --branch
git log --oneline cf2ff5cf078559e9843c34614318d80c984b4168..HEAD
git worktree add --detach "$LOCAL/code-old" cf2ff5cf078559e9843c34614318d80c984b4168
git worktree add --detach "$LOCAL/code-new" 4abbcafae69f2e3b9d162ad73e769913659aca59

env -i PATH="$PYTHON_BIN:/usr/bin:/bin" HOME="$LOCAL" LANG=C.UTF-8 \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$PYTHON" tests/evaluation/fixtures/offline_exec.py "$PYTHON" \
  docs/evidence/review-effects-preflight-2026-10-09/prepare.py --local-root "$LOCAL"

env -i PATH="$PYTHON_BIN:/usr/bin:/bin" HOME="$LOCAL/autonomous/home" \
  LANG=C.UTF-8 PYTHONPATH="$LOCAL/code-new" PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$PYTHON" tests/evaluation/fixtures/offline_exec.py "$PYTHON" \
  docs/evidence/review-effects-preflight-2026-10-09/autonomous-dry.py \
  --code "$LOCAL/code-new" --workspace "$LOCAL/autonomous/workspace" \
  --task-file docs/evidence/review-effects-preflight-2026-10-09/prepared/autonomous/task.txt \
  --output "$LOCAL/autonomous/dry-request.json"

ruff check docs/evidence/review-effects-preflight-2026-10-09/*.py
python docs/evidence/review-effects-preflight-2026-10-09/verify.py
git diff --cached --check
```

候选准备每个实际执行`python -m pytest -q tests`：各2 passed。独立G三组通过，D只在默认负数拒绝失败。自主任务初态`python -m pytest -q -p no:cacheprovider tests`：3 passed；独立验收1/9。初态验收失败是任务尚未实现的预期反例，不是Agent运行结果。

prepare首次仅u01因错误越界反例断言中断，修正后续跑完成；自主干跑的两个适配错误及保存范围见README。没有重复已通过的任务测试，没有真实模型重跑。离线请求不提供真实usage或线上成本。

文件隔离检查实际失败：

```text
bwrap --ro-bind / / --unshare-net -- /usr/bin/true
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
bwrap --ro-bind / / -- /usr/bin/true
bwrap: setting up uid map: Permission denied
```

在线运行命令尚未启用。还缺本轮明确账号/费用授权、宿主评分材料隔离、白名单Unix模型转发及冻结预算的硬限额核验。历史`run-nz.py`可作为已有生产入口参考，不能直接用它的24轮/旧预算覆盖本轮12轮提案。本轮三个脚本只有离线准备/干跑/核验入口，没有付费启动分支。

已有API key或历史余额不改变上述停止条件。`manifest.proposal_bounds`是待批准清单，不是新授权。
