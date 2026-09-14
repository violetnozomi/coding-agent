# 已读版本校验与陈旧编辑恢复

基线：NZ-Coder `df626c4f6c5a02408b229a6d99accbd6ca4724cc`；参考：InfCodeX `d3a812379b589597347f5be12d5b68477e577f02`。

| 参考行为 | 当前行为 | 差异与落点 | 对齐方式 | 验收 |
|---|---|---|---|---|
| managed task 创建 content hash cache；`read` 记录，`edit`/`write` 检查并在自身写入后更新 | `ToolExecutor` 已有按范围的读取去重缓存；文件工具已有同句柄身份与 CAS | 读取去重不代表模型读到的版本；写工具内部读取不能替代模型观测 | 在现有 `_ReadFileStateCache` 增加模型观测（完整内容哈希+展示范围），dispatch 前按文件校验；事务提交后更新自身基线 | 真实 `ToolExecutor` 回归覆盖 V1→V2、删除、行号偏移、touch 与同大小内容变化 |
| 编辑失败按未找到/多匹配等给出候选窗口 | NZ-Coder 已有 `edit_recovery` metadata 与恢复 hook | 保留既有锚点恢复；陈旧版本是独立原因 | 新增 `STALE_READ` 结构化结果和 `<stale-read-recovery>`，提供精确 `read_file` 入口；不自动修改文件 | 既有 AgentRunner 编辑恢复测试继续通过；陈旧写入字节不变 |
| 参考实现另算摘要 | `WorkspaceFileAccess` 可从同一句柄捕获身份 | 不重新读盘冒充模型已读 | `read_file` 在返回正文时写入 identity metadata；局部读取标记 offset/limit/complete | metadata 哈希与实际读取同源；partial read 不声称看过全文件 |

## 关键语义

模型通过 `read_file` 结果获得的身份是写入前的依据。写工具为了 CAS 进行的即时受控读取只负责竞争检测，不能刷新模型依据。内容哈希是主判断，因此仅 `touch` 不会被拒绝，而相同大小/时间戳的内容变化会被识别。没有历史观测的文件继续沿用原有创建和覆盖语义；读过后被删除则报告陈旧删除，不当作新文件。

陈旧拒绝发生在实际写工具 dispatch 前，返回 `executed=false`、`dispatch_failed=true`、`error_code=STALE_READ` 与结构化版本信息，不写入、不归类为锚点未找到、权限拒绝或 Provider 错误。恢复 hook 将原因和精确重读参数放入后续模型历史。成功事务提交后，只有已有观测文件的基线会更新，支持连续 `read → edit → edit`；失败、回滚、取消和 dry-run 不更新。

本轮只在现有读取缓存、ToolExecutor、ProductionToolResultProjector、恢复 hook 和事务生命周期上做衔接，没有引入版本服务、managed-task 框架或 Shell 文件监控。Windows 路径复用既有 `WorkspaceFileAccess` 分支，当前回归在 Unix 执行；Windows 未运行。
