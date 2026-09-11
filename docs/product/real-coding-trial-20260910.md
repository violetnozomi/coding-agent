# 两项真实 Pro 编码试用 — 2026-09-10

结论：配置合并 Bug 修复完成；CSV 功能任务未完成，且被组织者操作干扰，不能当作无干预能力样本。不是 2/2 成功，也不据此计算模型通用成功率。产品源码零修改，两项各一次正式运行，结束后未重跑、未修补 Agent 答案。

## 版本、授权与入口

- 产品源码 `4f4a5af5a035fa81554e0c836ebfb164087f318b`；试用开始分支 `codex/terminal-product-rc` / `f0be6ae083e3c247d144f4cff15ea580f868e83f`。两者 `nz_coder/` 无差异；后续 CI/dev 依赖修改不改变该候选产品。
- 候选 `terminal-4f4a5af`，软件版本 0.1.0。已安装 wheel SHA-256：`4b35dbdaa1fb4f3ed269484afad38058d3b004f8ef19a38b2ebdc049f4a66d3a`。
- 安装入口：正常用户数据目录的 `nz-coder/candidates/terminal-4f4a5af/venv/bin/nz-coder`，非 editable。源码外核对全部 392 个包文件与 wheel 一致；第一次在源码 cwd 内进行的 import 检查不作为安装证据。
- Python 3.13.12，OpenAI 3.11.0，httpx2 2.12.0，prompt_toolkit 3.0.53，Rich 15.0.0。未升级依赖、未重建包。
- 用户对“Pro、最多两项任务、新增 5 CNY 人工停止目标、不是金额硬限额”回复“可以，直接开始”，覆盖本轮。没有使用旧实验余额。
- 两次均通过真实 120×40 PTY、普通安装入口和输入框运行。启动器只读取此前允许的连接字段并 execve 产品，不调用 SDK/AgentRunner，不注入响应。独立 HOME/新 Session，未继承旧任务、答案或记忆。
- 实际配置快照断言：官方 `https://api.deepseek.com`，`openai-compatible`，`deepseek-v4-pro`，variant 默认空/null；10 turns、4000 output、32000 context；idle 60 秒、绝对 hard 600 秒；正常 `default` 权限；边界自测关闭。hard 仍包含本地等待。
- 凭据未进入转录或公开附件；无独立付费探针、无新依赖安装、无新验证 Agent。

## 任务与事先固定的检查

两项都是组织者本轮自编的小型标准库项目，不是现有开源 issue、P2/SWE-bench 题目，也不是用户生产仓库。需求与独立检查在出网前冻结；没有把独立检查或参考实现交给 Agent。测试证明有限任务完成情况，不代表外部真实 bug 分布。

CSV 任务：给现有 `python -m expensebook CSV路径` 增加 `--by-category`，输出 `category,total` CSV；strip 分类、空白归为“未分类”、按 Python 字符串升序、Decimal 精确累计、有符号金额、两位小数；支持中文、逗号分类、空格路径、只有表头；保持旧总额输出。输入保证两列及合法最多两位小数金额。要求 Agent 新增 CLI/单元测试、实际运行旧测试与新测试、如实回答。

配置任务：修复浅合并丢失嵌套兄弟字段；只在对应两侧均为 dict 时递归，其余由 override 整体替换（列表/null/false/0/空串）；保留单侧键；不修改输入，结果与输入不共享嵌套可变对象。输入限 JSON 数据、最外层 dict；保持 CLI 及原测试。要求 Agent 新增 CLI/单元测试并实际运行。

两条任务正文均限制只在当前仓库工作，不安装依赖、不用子 Agent、不外部浏览。

| 检查 | CSV 功能 | 配置合并 |
| --- | --- | --- |
| 初始 Git SHA | `0449a9624b67805e5c5435ac3ef47699028f62b5` | `2fa5ca0fa32337b235172fb6368dca18e6cdd713` |
| 初始原回归 | 2 passed，exit 0 | 2 passed，exit 0 |
| 初始独立检查 | 1 passed / 5 failed，exit 1 | 2 passed / 5 failed，exit 1 |
| Agent 产物 | 修改业务及 CLI、增加 9 个测试（总共 11） | 修改合并逻辑、增加 8 个测试（总共 10） |
| Agent 实际验证 | 仅 py_compile 完成；测试命令先被策略拦截，后两次被组织者拒绝 | 真实 unittest 10/10；另有 verify_changed_files 编译通过 |
| 最终原始 run 状态 | `max_turns`，不宣称完成 | `completed`，已给出最终回答 |
| 仅源代码补丁重放后独立检查 | 6/6，exit 0 | 7/7，exit 0 |
| 重放 Agent 自己的测试 | 10 passed / 1 error，exit 1 | 10 passed，exit 0 |
| 原始两项回归重放 | 2/2，exit 0 | 2/2，分别核对，exit 0 |
| sidecar | 未触发 | 真实 Pro / inherit-main / accept / verifier_ok |
| `/diff`、退出 | 四个源文件差异可见，CLI exit 0，termios 恢复 | 三个源文件差异可见，CLI exit 0，termios 恢复 |

## 必须保留的失败、干预及解释限制

CSV 首次测试调用 `call_00_iNXaTk4rMk92fwwCdizM2082` 请求：

```sh
python -m unittest discover -s tests -v 2>&1
```

Session 保留的真实返回是 `Error: Broad test runner blocked. A source diff already exists. Use verify_changed_files or run an exact/narrow test command if the task points to a specific failure.`，不是测试进程崩溃。工具 `executed=true` 表示进入工具执行边界，不能用来证明该 Shell 测试进程实际启动。

TUI 却显示 `Tool infrastructure failure; exit code unknown`。组织者先误判为内部故障，随后在两个精确测试命令的权限选择器中按 Ctrl+C，实际变成拒绝；Agent 在原有 10-turn 期限结束，状态为 `max_turns`，并未被立即取消。两次后续拒绝是组织者造成，不能标成模型自发失败。没有重新提交 CSV 任务。读清 Session 原因、确认运行已结束、usage 完整后，才继续原授权中的第二项任务。

CSV 还有独立可复现的测试编写错误：`test_groups_strips_and_sorts_ascending` 将空白分类样例写为 `"  \n-1.00\n"`，缺少分类与金额之间的逗号，违反题目保证的合法 CSV 输入。该新测试在未修改补丁的重放中报错。业务实现的 6/6 独立检查不能替代 Agent 自己新增并执行测试的完整要求；也不能推断如果未被打断，Agent 一定能或不能修好它。

配置任务的真实命令：

```sh
python -m pytest -q 2>&1 || python -m unittest discover -s tests -v 2>&1
```

安装环境没有 pytest，第一分支报缺包，第二分支实际执行 **10 项 unittest，0.044 秒，OK**。该工具 call `call_00_8z3V38WwsKzBDDU54CIx6160` 的 Shell metadata `exit=0`、`truncated=false`；ToolPart completed、`dispatch_failed=false`、`command_failed=false`。但同一命令的 `verification_result.status=failed`、消息 `_nz_verification_passed=false`；这个独立状态不一致保留，不被后续编译通过或 sidecar accept 改写。本轮不修该相邻机制。

配置任务的最终测试陈述有实际证据，但根因解释多说了一句：旧 `dict.update` 本来就能整体覆盖列表/null/false/0/空串；真正缺陷是嵌套递归与可变对象隔离，不是所有替换语义都错误。这是回答准确性的扣分项，不影响已验证的补丁功能。

人工权限等待显著污染耗时：CSV 前两次编辑工具总时长约 388.118 秒、102.703 秒；配置的首次编辑约 91.218 秒，测试文件写入约 222.084 秒。这些包含权限等待，不能归因为模型推理或文件 I/O 慢。后半段仅对已出现的白名单文件/测试权限弹窗发送普通 Allow once 键盘选择，没有切换 auto 权限。未对吞吐率或纯模型延迟作结论。

## 调用与费用

| 项目 | CSV | 配置合并 |
| --- | ---: | ---: |
| 主模型 / 辅助调用 | 10 / 0 | 8 / 1 |
| 实际 API attempts / retries | 10 / 0 | 9 / 0 |
| uncached input | 53,805 | 56,380 |
| cache-read input | 62,464 | 52,864 |
| output（不含 reasoning） | 2,279 | 2,955 |
| reasoning | 1,373 | 1,546 |
| cache-write | 0 | 0 |
| 总 tokens | 119,921 | 113,745 |
| 整轮 elapsed（含人工等待） | 1165.522 秒 | 515.165 秒 |
| 人工 CNY 估算 | 0.6015882 | 0.6448062 |

合计 **19 calls / 19 attempts / 0 API retries，233,666 tokens，约 1.2463944 CNY**。19 个 start/finish 配对，各自 usage 与 run_end 累计一致；每轮 tool call ID 唯一。第一项的后续模型迭代已计入，不隐去拒绝后产生的调用。

按此前保存的 2026-09-09 官方 Pro 峰时价格[^1]（每百万 uncached input 9、cache-hit 0.30、output 含 reasoning 27 CNY）估算；本轮未新查询费率或账户账单。公式为 `(input*9 + cache_read*0.30 + (output+reasoning)*27)/1e6`。本轮实际适用折扣/时段未核验，这不是精确账单、硬限额结算或账户余额。产品原生价格仍 unknown，不填零。5 CNY 是已披露的人工目标，不把余量视为后续授权。

## 可复现材料与范围

公开小型文本证据在 [evidence/coding-trial-20260910](evidence/coding-trial-20260910)：两组 initial/final patch 与冻结的 `acceptance.py`。final patch 为零上下文，重放时使用 `git apply --unidiff-zero`。在新的空目录应用 initial patch，再应用对应 final patch，可运行原 README 的 unittest 命令，以及 `python acceptance.py feature 项目目录` / `python acceptance.py bugfix 项目目录`。acceptance 应放在项目外。

本机私有现场以 `$TRIAL_ROOT` 指代，实际路径不写入公开报告，公开重放不依赖私有现场。现场保留 scope/frozen/config、启动器、两次原始 PTY、Session/trace、初始 Git、Agent 文件、最终回答及日志；这些私有文件不提交。Session/run：

- CSV：`session-20260910_223448-03fcddc8` / `20260910_223448_51b35446`。
- 配置：`session-20260910_230032-86f7f6ab` / `20260910_230032_ea8b3668`。

权威离线重放是每项 `source-replay/`、`*-source.log`、`source.patch` 与根目录 `source-result.json`。第一次归档也带入了工具生成的 pycache；原归档保留，但未用作最终依据。随后只取 `.py` 源码，从初始 Git 新建副本重放并禁用字节码缓存，得到表中结果。两个真实项目的 Git index 未改写；组织者未修改正式任务代码，没有模型/工具补执行。原历史真实现场未操作；候选 wheel 未更换。两轮退出均为产品真实 exit 0，前后 termios 相同，检查时无遗留 task-cwd 进程。

本轮仅限 Linux 本机、两个自编小任务。没有 Windows 实机真实编码、重启后继续编码、取消安全的新验证，未重跑历史全部离线套件，没有修复新发现的状态展示/策略问题。既有候选仍可使用，但这两次结果不足以宣称全平台稳定或通用解题能力提升。

### 提交前证据核对（2026-09-11）

使用现有候选 Python 在源码外执行本机 `verify_public.py`，exit 0；记录位于 `$TRIAL_ROOT/public-replay-wh39uyyw/`。该离线检查从全新目录重放公开补丁，确认上述成功及失败结果均可复现，重放后的所有 Python 源文件与真实现场逐字节一致，19 个调用的 usage 与 run_end 累计一致，两个任务工作目录无残留进程。检查 exit 0 表示证据吻合，不能理解为 CSV 的失败测试已修好。

同时核对冻结的独立检查文件哈希、候选 wheel 哈希和公开文件/两份 PTY 转录中未出现原授权凭据。收尾阶段新增真实模型调用 0、费用 0；不重建候选、不改产品源码，不启动后续任务。提交仅包含本报告和五个公开文本证据文件。

[^1]: DeepSeek. (2026). 官方模型与价格页面；复用此前保存的 2026-09-09 依据，本轮未重新查询。https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
