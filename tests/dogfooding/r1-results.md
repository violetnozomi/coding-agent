# 真实编码任务与前端体验验收 R1

## 结论

本轮修复了一个真实产品正确性缺陷：HTTP Native Runner 在工具执行后收尾失败、
丢失回复历史，而终端已显示完成。重新安装 wheel 后，相同权限、取消、续接场景
恢复正常。**不能据此判断真实编码能力或长期可用性**：当前执行环境无有效凭据，
四个真实模型编码任务均为 NOT_RUN；长输出错误详情与人工布局验收仍未通过。

## 版本、范围与运行环境

- 起点：`89124f9870e61a38290b1af1c97b0529da7188bb`，当时真实 origin/main。
- 分支：`codex/dogfood-product-r1`，独立 worktree；旧 main、分支、PR #2 未修改。
- 产品修复及回归提交：`7ca6146601b8470107f91dc99d5ed3c42a484c36`。
  后续报告提交不改变产品；最终 HEAD、PR URL 和 CI 以交付回复/本分支 PR 为准。
- Linux、Python 3.13.12、安装版 NZ-Coder 0.1.0。模块来自
  `<venv>/lib/python3.13/site-packages/nz_coder/`，移除 PYTHONPATH。
- 修复后 wheel SHA256：`a9cb7fae1814e6dfe2a23faa6f0b99c38d61aeb79061abcb6d5e243c27e974fc`。
  最终打包复建 wheel SHA256：`1eda9e856d77ac8eb7307d00ab465a293553387098926e7c3738516e3137525c`；
  再次安装后独立 HTTP 审批、文件内容、历史及复用冒烟通过。
  安装版与修复源码的 runner.py SHA256 均为
  `b72de14b686e27ecdfeea5946bfa32fd1685598817fe6aded579f562ee408023`。
- 产品仅删除结果 metadata 中无人消费的活体 publisher 引用；保留宿主/scoped bus
  事件绑定、权限、工具接口、事务与公开错误脱敏边界。没有新增产品依赖。

## 编码任务：0 次真实模型执行，1 次无凭据启动失败

T01 安装版 headless 在客户端构造阶段 exit 3：Missing credentials；没有发出请求。
过程环境和三个 workspace 配置快照均无 API_KEY。没有查找其他服务商、索取密钥、
复制凭据到项目或降低信任策略。T02–T04 按停止条件不发起。

原定 Provider/model：现有 `openai-compatible/deepseek-v4-flash`；不声称调用成功。
最大轮次 12、每响应最多 3 个工具、工具超时 30s、Provider deadline 90s、任务 600s、
串行、禁止子 Agent。辅助/verifier 调用也应计费，不能只统计 coding turn。
费用闸门的四条离线测试通过，但没有实际付费流量，不能称为真实账单验证。

| 任务 | 计划入口 | 实际终态 / 独立验收 | 修改文件 / 测试 | 干预及重试 | token / 费用 |
|---|---|---|---|---|---|
| T01 | 安装 wheel Headless | NOT_RUN；启动 BLOCKED_ENVIRONMENT；原始目标 5/7 条通过、整体 FAIL | 0；原有 1 个 unittest 通过；无新增测试 | 无，未重试 | 请求 0，消耗 0 |
| T02 | 本地 Terminal/TUI | NOT_RUN；原始目标 3/8、整体 FAIL | 0；原有测试通过；无新增测试 | 无 | 请求 0，消耗 0 |
| T03 | 同一 T02 进程/Session | NOT_RUN；前置 T02 未执行；原始目标 6/24、整体 FAIL | 0；原有测试通过；无新增测试 | 无 | 请求 0，消耗 0 |
| T04 | daemon + remote attach | NOT_RUN；原始目标 3/8、整体 FAIL | 0；原有测试通过；无新增测试 | 无 | 请求 0，消耗 0 |

因此没有模型成功率、token 效率或 SWE-bench 分数结论；不是 0/4 模型能力失败。
目标代码没有由执行者代写。模型任务难度与独立答案没有调整。

fixture 基线：T01 与独立 T02/T03 仓库均为
`b03fd40110f7bf41b1518fb996a4fe0998287e5f`；T04 为
`c28830eaf580d84481a87c2c34df192fd1613db5`。
准确任务原文、所有未完成源码和允许范围见 [fixtures.py](fixtures.py) 的 PROMPTS；
可见测试位于生成项目内，[accept.py](accept.py) 始终位于项目外。
这种隔离不是 OS sandbox。启动与未执行记录保留在私有状态目录。

## 四类真实产品交互

固定响应只用于 F 场景。真实安装版 CLI/daemon、认证 HTTP、Native Runner、
权限 dialog、文件工具和 Shell 进程执行；不是 Fake Runner 或手动构造渲染事件。
每组完整矩阵包含 8 个用户子请求：F01 三次审批，F02 慢工具+复用，F03 一次断开，
F04 长输出+中文多行复用。F05 标记仅表示同组复用探针，不是额外编码任务。

| 场景 | 修复前 | 同场景重装复测 | 独立证据 / 限制 |
|---|---|---|---|
| F01 权限 | dialog、拒绝、once 约束有效；收尾 failed | 通过已执行的行为项 | waiting_permission；对象是 permission-note.txt；Esc 后不存在；Enter 后写入；再次操作仍审批；拒绝结果进入工具回复 |
| F02 取消 | cancelled、进程已死；复用 failed | 通过已执行的行为项 | 正式 abort；终端 cancelled；所记录 PID 已不存在；late.txt 未生成；同 Session 下一请求 completed |
| F03 断开恢复 | 客户端退出时服务端 running；恢复后 failed | 服务端终态/单任务/续接通过；完整显示去重仅部分验证 | 实际终止 attach 客户端，再连接同 Session；工具完成；只有 1 条该用户请求和 1 个逻辑工具调用；cursor 前进；无永久 spinner。未逐帧证明显示恰好一次 |
| F04 长输出 | 工具失败卡片通用错误；收尾 failed | **整体 FAIL / 部分验证** | 实际产生 3,000 行、324,023 bytes；exit 7、truncated=true；终端有错误状态和最终摘要，但无明确截断说明，关键错误尾部未证实保留；50/100/110 列 resize 后还能输入 |

最后一组记录：取消到稳定状态探针约 0.716s（含一次 0.5s PTY drain，不是纯服务器取消
延迟）；取消及复用合计 1.528s。F04 所在累计 PTY 26,779 bytes，有界且仍可交互。
没有据此估算模型网络延迟、首 token 延迟或纯渲染性能。

请求接受、工具起止、assistant 终态时间与工具数量见
[r1-scene-metrics.json](r1-scene-metrics.json)。8 个子请求共 6 次实际工具调用，
13 个可见 assistant turn；辅助 Provider 调用总数未逐请求记录，明确 UNKNOWN。
固定适配器不联网，实际付费为 0；返回的 0 token 是合成值，不能用于效率判断。
首条前端有意义反馈和前端最后绘制时间没有可靠独立时钟，记为 UNKNOWN。

所有前端结论最多 PARTIALLY_VERIFIED（PTY/文本和状态证据）。
人工布局、CJK 单元格对齐、窗口 resize 后遮挡、完整视觉去重均 NOT_VERIFIED。
没有 Windows/ConPTY 实机结论。

## 所有执行与纠正记录

1. 无模型的安装/daemon readiness 探针；未记为 F 成功。
2. 初次部分场景：F01 的三个请求 + F02 启动。收尾失败使连续用户消息被合并，
   验收适配器错误选择旧 F01 标签；F02 当次无效。保留原始输出，修正标签选取。
3. `first-matrix`：未改产品，完整 F01–F04，冻结所有首次结果。
4. 离线定位：1 次 SDK、1 次实际 HTTP 运行，证实只有传入活体 bus 的链路收尾失败。
   另有参数/import/workspace 设置错误在运行前失败，不列为产品缺陷。
5. 修复前回归 2 failed；最小修复后 2 passed；重建、重装。
6. `fix1-matrix`：完整 F01–F04 重测。
7. 独立代码复核发现证据脚本两处假阳性：错误标记存在于命令本身，final 标记可来自
   前一请求。保留旧记录并取消其成功含义；取消计时原本包含复用，也已更正标签。
8. `final-matrix`：仅纠正测量，不改产品，完整 F01–F04 再测。
9. `release-smoke`：最终复建 wheel 再次安装，在源码外用正式 daemon/HTTP 执行
   F01 单次允许写入及同 Session 复用；独立检查文件内容、两条 assistant 回复、
   completed 状态和 --help exit 0。此为额外的两请求打包冒烟，不是 T01–T04。

合计：真实模型编码执行 0；完整确定性矩阵 3 组 + 部分矩阵 1 组，
14 个场景调用（其中 F02 首次无效），28 个场景用户子请求；另有 2 次离线定位运行。
最终打包冒烟另计 2 个确定性 HTTP 请求。
pytest 不混入产品场景数量。所有失败和重测都保留，没有挑选最好结果。

## 问题与修复边界

| 类别 | 发现 | 本轮处理 |
|---|---|---|
| A 正确性 | live publisher 进入 metadata；_typed_result deepcopy 失败；完成/失败状态与历史分叉 | **已修复 1 项**，SDK+真实 HTTP Native Runner 回归和安装版三类恢复场景支持 |
| B 易用性 | 非零 Shell 命令卡片只显示通用错误，截断说明不可见，关键尾部缺乏有效显示证据 | 保留未完成项；公开错误脱敏是已接受安全边界，不在本轮改成透传原始错误。不能写 F04 通过 |
| C 模型能力 | 无可用真实模型样本 | 不作判断 |
| D 环境 | 无有效 API 凭据；无人工终端视觉/Windows 证据 | 按停止条件记录，不归咎于 Agent Core |

独立审查未发现本次生产修复的 Critical/Important 问题；确认事件绑定及所有权未改变。
它指出的证据缺陷均已注明，没有把 PTY 字符串命中当成功。

## 验证命令与 exit code

命令在本分支 worktree 执行，私有日志不进入 Git。

| 实际命令 | 结果 / exit |
|---|---|
| `python -m pytest -q`（基线） | 3751 passed, 35 skipped；287.59s；0 |
| `python -m pytest -q tests/dogfooding/test_http_settlement.py`（修复前） | 2 failed；1 |
| 同一命令（修复后） | 2 passed；0 |
| `python -m pytest -q`（修复后全量） | 3758 passed, 35 skipped；279.89s；0 |
| `python -m pytest -q tests/dogfooding`（最终脚本复核） | 7 passed；0 |
| `git diff --check` / `git diff --cached --check` | 0 |
| `python -m compileall -q nz_coder tests` | 0 |
| `python -m ruff check nz_coder tests` | All checks passed；0 |
| `python -m build --wheel --sdist` | 构建成功；0 |
| `<venv>/bin/python -m pip install --force-reinstall --no-deps <wheel>` | 0 |
| `<venv>/bin/python -m nz_coder --help`（源码外） | 0 |
| `<venv>/bin/python tests/dogfooding/offline.py <fresh-private-attempt>` | 安装模块，最终矩阵完整退出 0；**不是所有验收项都 PASS** |
| `<venv>/bin/python -`（源码外最终打包冒烟，调用 start_daemon / create_session / run / reply_permission / messages / stop_daemon） | 等待审批、once 写入内容和回复历史均独立断言；同 Session completed；0 |

可重建顺序：构建安装 core wheel → 安装 `tests/dogfooding/provider` 验收专用扩展 →
生成独立 fixture → 外置 accept.py 验证基线 → 凭据/预算满足才运行真实任务；
offline.py 仅用于恢复场景，路径参数必须是新的私有证据目录。
扩展不作为产品依赖发布。结果脚本拒绝覆盖已有 attempt 记录。

原始材料保留于用户私有 state 下本轮目录：baseline/final pytest、构建安装日志、
T01 启动错误、fixture manifest/原始验收、三个矩阵的 snapshot/PTY、原始部分场景。
仓库只有必要脚本、冻结结果及数字摘要，无 key、环境快照、原始 reasoning 或私人路径。

本轮新 PR 不自动合并。最终 SHA 的远端检查在 push 后读取；尚未结束的任务写 pending，
不引用 PR #2 的绿色 CI 作为本轮证据。

## 下一阶段只建议一件事

**在配置可用 Provider 后完成这四个冻结编码任务，并由使用者实际检查终端画面，
再决定是否适合日常使用。** 当前证据不足以给出“核心和终端产品已全面验收”的结论。
