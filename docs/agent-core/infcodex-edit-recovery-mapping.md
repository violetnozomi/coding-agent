# InfCodeX 与 NZ-Coder 精确编辑恢复对照

对照版本：InfCodeX `Tokfinity/InfCodeX@d3a812379b589597347f5be12d5b68477e577f02`；NZ-Coder `7833a47`。

| 参考行为（实际调用点） | 当前 NZ-Coder 行为 | 真实差异 | 修改位置 | 标签 | 验收场景 |
|---|---|---|---|---|---|
| `applyPostToolProcessing` 在真实工具结果之后调用 `buildEditRecoveryUserMessage`，将恢复消息加入后续历史（`agent-runtime/tool-dispatch.ts:614-680`） | Tool Runtime 已有 `after_result` 钩子，但编辑工具失败只返回普通字符串，通用诊断按文本判断 | 编辑失败的结构化事实没有进入文件级恢复状态，Bash 相同字样也可能触发旧提示 | `tools/files.py`、`verification/hooks.py`、`verification/recovery.py` | 本项目适配 | 真实 AgentRunner 第二次请求含编辑恢复证据 |
| `toolEdit` 区分 `EDIT_NOT_FOUND`、`EDIT_AMBIGUOUS`、`EDIT_TOO_LARGE` 并保留失败原因（`tools/edit.ts`） | `edit_file`/`apply_patch` 已有精确匹配、事务和 CAS；失败文本没有统一恢复 metadata | 保留兼容文本，同时增加错误码、文件版本和候选窗口 | `tools/files.py` | 直接对齐 + 本项目适配 | 未找到、多匹配不写入并返回不同分类 |
| `inspectEditFailure` 返回有界候选行窗口和 excerpt，恢复消息给出下一步精确编辑（`tools/text-anchor.ts`、`edit-recovery.ts`） | 已有 `_nearby_context`，但只生成展示文本 | 增加行号、原文窗口、候选序列和 `read_file` 参数；无候选时明确精确重读 | `tools/files.py`、`verification/recovery.py` | 本项目适配 | 重复前缀、过期片段和非 Python 文本 |
| 恢复状态按文件记录，失败时阻止同文件现有文件 `write`，成功编辑后清理（`edit-recovery.ts`、`tool-outcome-tracking.ts`） | 原有 `RecoveryState` 只记录通用错误和重复调用 | 在现有 run-scoped RecoveryState 中按规范化路径记录，外部文件变化使旧限制失效 | `verification/recovery.py`、`tool_runtime/policy.py`、`verification/hooks.py` | 本项目适配 | 同文件覆盖被阻止，精确编辑成功后解除，其他/新文件不受影响 |
| 编辑失败只由编辑工具的错误分类触发，不把普通工具输出当编辑恢复（`parseEditToolError`） | 通用诊断此前按 `old_text` 文本片段触发 | 结构化 `edit_recovery` metadata 成为唯一编辑恢复入口，Bash 相同字样不建状态 | `verification/hooks.py`、`verification/recovery.py` | 直接对齐 | Bash 文本包含 old_text 不触发编辑恢复 |

本轮选择性适配了 InfCodeX 的恢复控制流和证据形状，没有移植其 TypeScript substrate、固定窗口数值或 Unicode 写入回退。候选只用于定位，真正写入仍由模型通过现有唯一 `edit_file`/`apply_patch`、事务和 CAS 完成。
