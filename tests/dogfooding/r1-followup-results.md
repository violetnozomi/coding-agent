# R1 补验：真实 Provider 与 F04 安全诊断（2026-09-06）

## 结论与版本

**F04 的已执行自动化行为项通过；人工视觉未验。T01–T04 全部 NOT_RUN，不能判断编码能力。**
本轮只修改 Shell 诊断的采集出口、公开投影、事实传递和终端恢复入口，未改 Agent 策略。

- 基线：`6939f05a3d272c752e8ad8011730c51ee7f01e4b`，PR #3 尚未合并。
- 分支：`codex/dogfood-r1-followup`；新 PR 基于 `codex/dogfood-product-r1`，依赖 PR #3。
- 产品及回归提交：`6b08a8073f7b0a3d8ba8188bd71e4cb23a731b7f`；后续只提交本报告与机器记录。
  最终交付 SHA、PR URL 和该 SHA 的 CI 状态在随附 PR 说明/交付回复列出，避免提交自引用；不自动合并。
- 最终 wheel SHA256：`e869b97de764455c9398b942873870198df4b9aa7809f2c7ea43d16b38bc570e`。
- sdist SHA256：`d6eef8895e4fba4df27d83866e468e7d3a3d90594e8468179829bcd4a7a15dc3`。
- Linux / Python 3.13.12；源码外独立 venv 安装。wheel 中 374 个 Python 文件与最终源码逐字节一致。
  旧 R1 报告、fixtures.py、accept.py、任务原文及允许修改范围保持只读。

## A：凭据与四个编码任务

本轮开始及交付前配置快照均无 API_KEY/OPENAI_API_KEY，有效 credential 来源为 default。
没有寻找其他目录的凭据，没有启动注定失败的编码任务，也没有连接探针。
真实适配器未调用；真实 Provider 请求、token 与费用均为 0；没有有效模型样本。
固定响应扩展仅安装到独立的 B 场景 venv，不是产品依赖，更不是编码任务的替代模型。

| 任务 | 产品/独立验收 | diff / 人工干预 / 重试 |
|---|---|---|
| T01 Headless | NOT_RUN / NOT_RUN | 无修改、无代写、0 次 |
| T02 本地 TUI | NOT_RUN / NOT_RUN | 无修改、无代写、0 次 |
| T03 同进程/Session | NOT_RUN / NOT_RUN；T02 前置不足 | 无修改、无代写、0 次 |
| T04 daemon/attach | NOT_RUN / NOT_RUN | 无修改、无代写、0 次 |

原始 fixture 的 FAIL 不计作模型失败。后续如有凭据，仍需原冻结任务、原预算闸门：
总额不超过 $5、至多 6 次编码尝试与 1 次小探针，含辅助请求、重试和失败调用。
本轮没有验证真实账单或预算闸门的线上效果。

本地配置的最短支持路径：运行 `nz-coder` → `/connect` → 选择 OpenAI-compatible，
在隐藏输入框填写 key，endpoint 填 `https://api.deepseek.com` →
`/model openai-compatible/deepseek-v4-flash`。凭据进入用户私有配置，不放 workspace `.env`，
不发送到聊天或提交 Git。配置完成后另行按冻结预算运行 T01–T04。

## B：根因、修复与证据

原始 stdout/stderr 合流，采集器保留有界 head/tail。旧场景的 stderr 可能先于缓冲 stdout
刷出，且标记写在命令里，不能据此判断“丢尾部”。本轮受控脚本先 flush stdout，
执行时随机生成错误行号，独立写出期望值；命令和固定最终回复都不包含该行号。

确认的链路问题：完成事件漏传 exit/truncated；非零事件被统一覆盖成通用错误；
失败 ToolPart 的事实在 normalization/settlement 丢失；snapshot 恢复未生成失败卡片；
已结束会话的 attach 入口根本未调用 snapshot 恢复。

修复复用 `exit`、`truncated`、字节数，兼容新增闭集 diagnostic/termination 元数据。
命令失败、基础设施异常、超时和取消分开显示；未知不填 0/false。
只重建固定 Python SyntaxError/IndentationError/AssertionError 类别、行号及部分
unittest/pytest 计数，不输出路径、源代码行、任意异常消息或任意 stderr。
运行中仅公布字节进度；成功结算后保留原有正常输出；未知失败诊断明确隐藏。
原有 typed public message 保留，未将任意 stderr 标记为 TrustedPublicMessage。

| F04 子项 | 旧安装版有效首次 | 中间安装版 | 最终安装版 |
|---|---|---|---|
| exit 7 / 截断事实 | snapshot 有、卡片无 | 显示通过 | 显示通过 |
| 安全诊断及行号 | 模型/snapshot 原始输出有行号，卡片无 | 通过 | 通过 |
| 合成 secret 不出现在公开/模型输出 | 不通过 | 通过 | 通过 |
| 已结束 Session 实际重连显示卡片 | 不通过 | 不通过 | 通过 |
| HTTP 写文件及同 Session 续接 | 未追加运行 | 通过 | 通过 |

最终身份见 [机器记录](r1-followup-metrics.json)：session、interaction、tool call、attempt
四项关联。A 为测试专用透传观察器记录的真实安装版 `run_bash` 返回 ToolOutput，
位于公共结果投影之前；B 为权威 snapshot；C 为当前请求独立 PTY 片段，另存重连片段。
观察器执行原 handler 一次、返回同一对象，不改权限或工具结果；**没有宣称观察了私有原始缓冲区**。
辅助的真实 Shell 回归另行验证有界采集、尾部、安全输出与模型接收路径。

最终产生 309,458 原始合流 bytes，保留文本 UTF-8 编码 4,244 bytes，exit=7、truncated=true；
主请求 PTY 12,950 bytes；没有由此推断布局、token 效率或渲染延迟。
最终 B 场景含 6 次离线适配器调用（含辅助调用），费用 0，合成 token 不参与能力统计。

保留全部尝试：首次 driver 因 workspace 重叠未启动；第二次误定位合成反馈而重复申请审批，
超时退出并产生 shutdown 警告；修正后旧 wheel 完整 F04 一次；中间 wheel 一次；最终 wheel 一次。
没有重跑完整 F01–F04 矩阵。旧 wheel 的 model/snapshot 标记比较最初错误要求新格式，
后用独立行号重算为“尾部存在”；原记录不覆盖，纠正在机器记录中单列。

## 验证与剩余范围

回归从原实现 11 failed 开始；后续缺陷分别补 Red–Green，包含真实 HTTP/Native Runner
收尾、真实超时结算、无 replay 的实际 attach。审查发现的字段覆盖和兼容性回归已修复。
本地 gap 用例曾缺必需 Part 身份，已纠正；attach 初版断言会命中历史回复，现要求真实卡片标题。

| 命令 | 最终结果 / exit |
|---|---|
| `git diff --check` | 通过 / 0 |
| `python -m compileall -q nz_coder tests` | 通过 / 0 |
| `python -m ruff check nz_coder tests` | 通过 / 0 |
| 定向 pytest，完整参数见机器记录 | 195 passed / 0 |
| `python -m pytest -q` | 3781 passed, 35 skipped；462.69s / 0 |
| `python -m build --wheel --sdist` | 成功 / 0 |
| 源码外安装 wheel + `f04_followup.py <fresh-private-dir>` | F04、重连、HTTP 文件及历史续接通过 / 0 |

初次全量为 3 failed、3766 passed、35 skipped，已修复；一次为补真实调用链主动停止，
3658 passed、35 skipped、exit 2；其后中间版本全量 3780 passed、35 skipped、exit 0。
最终 CI 只读取新 PR 最终 SHA，不借用 PR #2/#3 的结果；未完成的 job 记 pending。

人工检查全部 **NOT_VERIFIED**：按正常终端启动后观察审批、失败卡片、输入框，
调整 50/100/110 列，检查中文与代码块、遮挡、重连重复及答复归属。
本轮只有 PTY/文本自动化，不是真人确认；未做 Windows/ConPTY 实机补验。
任意自由格式 tail 仍隐藏，unittest errors-only 等未覆盖格式仍可能无详细摘要。

合并判断分开：代码修复已有全量/定向测试、安装链路和独立复核依据，远端 CI 单独核验；
真实编码能力无样本；终端本轮自动化行为通过，但人工视觉与真实编码验收尚未完成。
