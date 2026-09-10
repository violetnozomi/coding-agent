# NZ-Coder Linux 个人内测候选

候选：`terminal-4f4a5af`（源码 `4f4a5af5a035fa81554e0c836ebfb164087f318b`，版本 0.1.0）。适用于 Linux 本机终端。边界自测增强默认关闭，权限模式为正常 `default`。

## 开始使用

将候选目录记为 `$NZ_CODER_CANDIDATE`（本机默认位于 `$XDG_DATA_HOME/nz-coder/candidates/terminal-4f4a5af`，未设置时为 `$HOME/.local/share/nz-coder/candidates/terminal-4f4a5af`），进入自己的代码仓库后运行：

```bash
"$NZ_CODER_CANDIDATE/venv/bin/nz-coder"
```

或临时激活环境：

```bash
source "$NZ_CODER_CANDIDATE/venv/bin/activate"
nz-coder
```

首次使用可在终端执行 `/connect` 配置账户，使用 `/model` 核对模型，`/status` 查看工作目录、Session 和权限模式。连接和模型请求可能产生费用；不要把密钥发送给他人。

## 日常操作

- 在输入框提交任务，按权限选择器逐次批准需要的操作。
- `/diff` 查看本 Session 已记录的 Agent 修改；它是历史快照，不是全仓实时 `git diff`。
- `Ctrl+C` 取消当前输入或运行中的任务；`/sessions`（或 `/session`）选择并恢复会话。
- `/undo`、`/redo` 仅覆盖产品记录的文件变更，不承诺撤销任意 Shell/MCP 外部副作用。查看历史 diff 不会自动重放或修改文件。
- `/exit` 正常退出；恢复后可先用 `/status`、`/diff` 审阅，再继续工作。

## 反馈

请提供候选 ID、Session/run ID、操作步骤、预期与实际表现、文件或任务是否改变，以及已有诊断信息。不要附带 API key、完整环境或完整模型正文。

| 候选版本 | 任务简述 | 卡住步骤 | 预期/实际 | 安全影响 | Session/run | 临时处理 |
|---|---|---|---|---|---|---|
| | | | | | | |

范围限制：目前只承诺 Linux 本机终端路径；Windows/HTTP、鼠标/剪贴板等未在本轮验证。产品价格未知，不是零费用，普通 TUI 也没有已验证的金额硬上限。历史验收证据见 `terminal-rc.md`、`terminal-stream-live-recheck.md` 与 `terminal-restored-diff-fix.md`。
