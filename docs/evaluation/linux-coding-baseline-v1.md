# Linux 真实编码任务基线 V1 — P0 离线准备

## 结论与授权边界

本轮交付为 **A：离线任务准备与接线检查完成，真实基线未执行**。
没有实例化真实 Provider，没有发出付费模型请求，也没有生成真实编码成绩。
本轮没有修改 `nz_coder/`、提示词、完成门控、权限、检索或 HTTP 服务。

此前会话选择过 DeepSeek，后又有其他 Provider 的讨论；这不等于本轮已冻结模型及总费用。
目前缺少本轮明确的付费授权，尤其是**总费用上限及币种**。
预选试运行题为 **T01、T04**，来自固定 12 题，不按结果换题。
建议沿用之前的 `openai-compatible/deepseek-v4-flash`，先串行试运行这两题，
再在相同版本、配置、授权余额内运行其余 10 题。模型/effort 若已变更，应在启动前明确。
单题暂拟 30 轮、600 秒、100,000 token；费用额度及是否可在总预算内分配尚未授权。

重要区别：本次已验证的 `RequestBudget` 是**离线保守预留协议**，不是 DeepSeek 真实计费适配器。
`--live` 当前明确拒绝执行。收到授权后，仍须先按确认的模型和价格冻结真实调用配置，
接入并验证每次请求（包括 Provider 内部重试及辅助模型）的上界预留，再开放 P1。
不能把 Fake credits 换个名字就当美元计费，也不能直接绕过驱动手工运行 12 次 CLI 后声称预算受控。
这是 P1 的待办，不是已经验证的能力；本轮没有为了等待授权新增 Provider 层架构。

## 冻结版本

| 对象 | 本轮值与含义 |
| --- | --- |
| agent_revision | `7c308e3a75deae112e20c0de225113fda6ec9f9e`，包含上一阶段封存成果 |
| harness_revision | `dc179b07411bc3ff45830db7fbd4fd8bf459b8d8`，任务、参考校验、薄驱动、自检代码 |
| task_revision | 各初始文件树的 SHA-256，见 manifest；确定性初始 Git commit 见 preparation |
| acceptance_revision | 各独立目标断言文本的 SHA-256，见 manifest |
| experiment_id | `linux-baseline-v1-p0-dc179b0`，仅 offline/preparation |
| 实际模型配置 | **未授权、未冻结、未运行**；离线替身为 `offline/offline-model`，无 effort |

任务 [manifest](../../evaluation/linux_baseline/results/p0-dc179b0/manifest.json) 由
[catalog.py](../../evaluation/linux_baseline/catalog.py) 生成；独立目标断言也在该文件中。
两个 fixture 的同源任务可以具有相同初始树，
但需求和独立验收分别固定。不得把 task revision 与 Agent revision 混为一谈。
结果提交可以晚于驱动提交；结果中的 harness SHA 指向真正执行的代码，而不是自引用的报告提交。

本机基础环境为 Linux / Python 3.13.12；公开 summary 记录 pytest、OpenAI 客户端等具体版本。
本次复用已安装环境，没有安装新依赖或修改系统 Python。
其中 pytest 为 9.0.3，高于项目开发 extra 声明的 `<9`；这是本轮实际测试环境的限制，
不宣称验证了项目声明的完整版本矩阵。正式运行须沿用冻结环境，或在运行前重新记录环境版本。

## 确认的产品入口

入口由 `pyproject.toml` 的 `nz-coder` console script 指向 `interface.cli:main`。
已实际执行 `python -m nz_coder run --help`，退出码 0。
调用链为 `CLI main → headless.run_main → AgentClient → NativeSDKRunner → ProductRunEnvironment → AgentRunner`。
没有通过 daemon/HTTP，也没有自行实现模型—工具循环。

本轮测量对象是 **Linux 非交互 headless CLI**，不是完整交互式终端体验。
离线检查保留了真实注册工具、ToolRuntime、上下文服务、MemoryService、CompletionVerifier、
SessionRuntime 及持久化；只替换模型响应边界，并禁止 legacy `AgentLoop` 构造。
环境工厂的观察包装只记录实际类型，不替换服务实现。

具体配置是 `MAIN_PROFILE`、`declared-agent-graph`、`stream=False`、`permission-mode=auto`、持久 Session。
系统指令来自正式 `runtime.conversation.prompt.build(memory_block="", skill_descriptions="")`；
不是临时的“你是编程助手”。每次模型请求记录工作区路径归一化后的系统指令哈希、消息角色及工具 schema 名单。
正式产品仍保留运行内记忆/压缩和完成验证机制。

交互式 CLI 的 coding assembly 与 headless 的 declared assembly 不同，不能因为共用 Runner 就宣称配置等价。
离线固定关闭外部 MCP、规划和反思开关，未改其他产品策略。
MAIN profile 本身仍支持 child agent，动态工具发现也仍存在；Fake 脚本没有调用这些能力。
离线工具名单不能替代真实 Provider 能力解析后的工具范围；P1 前必须重新冻结实际能力与所有可能计费的模型。

### 旧 eval_runner 的核对

已阅读 `EVAL.md`、`evaluation/eval_runner.py`、CLI、composition、native_sdk、core/request。
旧 runner 会设置全局 `memory_mgr.memory_dir = repo / '.nz-coder' / 'memory'` 并加载全局记忆，
其默认流程调用 `agent.change_tracker.revert()`；旧 `_git_diff` 记录名称/差异字符，
不交付包含未跟踪新文件的可重放最终补丁。因此本轮没有调用或大改它。
新驱动只负责任务副本、调用正式入口、留存和独立 pytest 验收。

## 固定任务集与逐题验收口径

两个代码库均为**可信 synthetic/local fixtures**，不是大型开源项目采样。
textkit 的空输入任务沿用本地 `examples/eval_repos/demo_python/demo_pkg/parser.py` 的旧演示缺陷思想；
其余功能围绕文字处理流水线和 Decimal/JSON 工作日志重新构造。
正式任务仅应向模型提供需求及公开开发测试，不提供隐藏断言或参考补丁；
本轮 Fake 接线甚至没有提交这 12 题的解题需求，只使用独立的离线 probe 指令。

| ID | 类型 | 仓库 | 需求 | 真实最终验收 |
| --- | --- | --- | --- | --- |
| T01 | 单文件 | textkit | 空白配置输入解析 | not_run |
| T02 | 单文件 | worklog | Decimal 精确求和 | not_run |
| T03 | 单文件 | textkit | 词频并列顺序确定性 | not_run |
| T04 | 跨文件 | worklog | project 字段经过 JSON 与 CLI 完整传递 | not_run |
| T05 | 跨文件 | textkit | lower 选项贯通流水线与 CLI | not_run |
| T06 | 跨文件 | worklog | archived 开关贯通选择器与 CLI | not_run |
| T07 | 小功能 | textkit | Unicode slug 模块与公共导出 | not_run |
| T08 | 小功能 | worklog | 项目分组总时数及 JSON summary | not_run |
| T09 | 小功能 | textkit | 保序、缺省值、迭代器列投影 | not_run |
| T10 | 边界回归 | worklog | 空输入和耗尽迭代器的平均值 | not_run |
| T11 | 边界回归 | textkit | 迭代或校验失败时不破坏输出文件 | not_run |
| T12 | 边界回归 | worklog | 批量追加验证原子性及自身追加 | not_run |

每题 manifest 包含需求、来源、初始树哈希、公开开发测试、独立验收位置、预算草案及允许/禁止修改范围。
单文件题限定具体源文件；其他题限定该包；所有题允许添加开发测试，禁止修改 README、Git 设置及工作区外文件。
导出补丁后检查修改范围，不能只以测试通过认定合格。

准备时每题执行两组独立检查：初始目标测试必须有预期失败且没有收集/导入错误；原有回归必须通过。
随后**只在另一个 organizer-reference 副本**应用人工参考修复，导出完整 patch，
再到第三个干净副本重放并执行目标+原有回归，以确认题目可解、验收和补丁链路有效。
这些参考结果绝不记作 NZ-Coder 成绩，也不会把参考仓库交给 Agent。
零测试、全部跳过、收集失败、非零退出、超时或清理异常都不能被记录为验收通过。

冻结后 **12/12 题有效**：初始目标共 33 个预期失败、7 个通过，原有回归 48 个通过；
独立参考补丁重放后，40 个目标检查与 48 个原有回归全部通过。
每题详细计数及退出码见 [preparation.json](../../evaluation/linux_baseline/results/p0-dc179b0/preparation.json)。
这些数字描述的是题目有效性，不是模型解题率。

## 隔离与证据边界

每题从初始文件树新建独立 Git 仓库，使用新子进程和唯一 Session。
HOME、XDG state/cache/config、临时目录全部按 attempt 独立分配；不继承开发者 API key、代理、旧会话、记忆或 pytest 插件环境。
通过正式 `WorkspaceTrustStore` 仅信任该可信 fixture 的当前控制指纹，信任记录写入该题私有 HOME，不修改真实用户配置。
子进程只通过正式模型/工具执行链改文件；评测组织者不修改正式任务副本来提高成绩。

观察到 Session、Memory、Trace、ToolResult 和主索引缓存的派生路径均位于独立 HOME。
现有产品另外生成 workspace-local `.nz-coder/index`；本轮没有清退这个辅助索引目录。
它按题隔离，且 `.nz-coder` 受现有 WorkspacePathPolicy 的模型读写隐藏规则保护。
不能因此声称所有内部缓存都已经迁到工作区外，也不能声称 Shell 是 OS sandbox。
普通临时目录不防恶意代码读取宿主其他路径；本轮仅运行自建可信任务。

隐藏目标测试、原有回归副本、参考实现、JUnit XML 和验收日志都在 evaluator 目录，不进入模型工作区或提示词。
Agent 自己的自测只作为过程证据；独立验收重放 patch 后运行的是评测侧冻结的原有回归，
不使用 Agent 可能修改过的 public test 来替代原有回归。

最终文件先使用**仅针对自有 fixture**的 `git add --all` 纳入新文件，再以 `git diff --cached --binary --full-index HEAD` 导出。
回归测试验证了未跟踪新文件、删除、二进制内容和可执行位的重放。
生成缓存按 fixture 的固定 `.gitignore` 排除；运行目录和原始现场仍保留，未执行自动回退或清理。
即使启动、读取结果或验收异常，也先保存 result 和可导出的 patch，不能把失败现场直接删掉。
进程退出/超时后只终止新建的自有 process group；这不构成防主动脱离进程组的恶意进程隔离。

## 结果：没有真实分数

| 指标 | 真实任务数 |
| --- | ---: |
| 计划 | 12 |
| 启动 / 正常完成 | 0 / 0 |
| 端到端成功 / 独立最终验收通过 | 0 / 0 |
| 已启动失败 | 0 |
| 未运行 | 12 |
| 已启动但用量缺失 | 0 |

十二条真实结果均为 `not_run`，`usage/cost/elapsed/patch_verified/runtime_completed/within_budget` 为 null。
真实成功率为 null，不写成 0% 或 100%。没有真实调用，所以本轮新增 Provider 消耗为 0；
这不表示未来请求免费，也不表示未运行任务具有零 token 或零成本。
没有成功/失败真实任务，因此这两组的成本、耗时分布均无样本。

原始 Fake 用量和 fake credits 仅验证接线，不与真实用量相加。
两次写入 probe 经过真实完成门控停在 `max_turns`：虽然 Bash public tests 实际通过，
仍不能从成功工具输出推断整次运行完成；保留为 offline `budget_exceeded`。
第三次 probe 只允许一次 Fake 响应，下一请求预留被拒绝，未发生第二次响应生成或文件写入。
这是离线停止机制证据，不是实际 Provider 计费/取消的零超支保证。

当前 JSONL 公共事件的 tool finished payload 不含完整结果状态。
报告中的 `tool_failed_events` 仅是失败事件数量，**不能当作所有 dispatch_failed/command_failed 的准确计数**。
完整原始会话/轨迹只在本地保留；真实测试前需要按最终确认的调用链核实工具结果与重试/usage 来源，
缺失项保持 unknown，不能据事件缺席填 0。

## 执行证据与可复现命令

冻结驱动前，执行：

```bash
env -u API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
  tests/evaluation/test_linux_baseline.py tests/test_headless_cli.py --tb=short \
  --basetemp="$PWD/.nz-coder-runs/p0-test-fresh-directory"
python -m ruff check evaluation/linux_baseline tests/evaluation/test_linux_baseline.py --output-format concise
python -m nz_coder run --help
```

相关测试 **55 passed，退出码 0**；Ruff 通过，退出码 0；入口帮助退出码 0。
测试使用的实际 basetemp 位于项目本地 `.nz-coder-runs/linux-baseline-v1-p0-tests-2`，未覆盖用户工作目录。
历史离线驱动自检失败保留：先是误把 `.nz-coder/index` 的存在当作私有 Session 没有迁移；
随后是把 Fake 完成门控 `max_turns` 错当成必须 completed 的接线标准。断言修正为核对真实状态与准确存储位置，
没有修改产品使其绕过门控。最初 dev probe 还包含评测脚本的错误辅助函数 import，已删掉该无用 import。

冻结后的完整 P0 命令（输出目录必须是新目录，避免覆盖历史证据）：

```bash
python -m evaluation.linux_baseline.runner \
  --output "$PWD/.nz-coder-runs/p0-fresh-experiment" \
  --dry-run --publish evaluation/linux_baseline/results/p0-fresh-experiment
```

上述冻结 P0 命令实际退出码 **0**，`harness_dirty=false`、`initial_tasks_valid=12`、`offline_wiring_valid=true`。
命令和退出码摘要另存 [verification.json](../../evaluation/linux_baseline/results/p0-dc179b0/verification.json)。
本次实际实验名与指标见公开 summary；原始输出在项目本地
`.nz-coder-runs/linux-baseline-v1-p0-dc179b0/`，没有只留在 `/tmp`。
公开目录为 [evaluation/linux_baseline/results/p0-dc179b0](../../evaluation/linux_baseline/results/p0-dc179b0)。
其中 manifest、preparation、results、summary 及三个 offline patch 可长期复核。
公开投影不含模型私有推理、工具参数/正文、最终回答、凭据或私有宿主绝对路径。
参考 patch 和未经裁剪的原始日志留本地，不作为公开模型轨迹。

单个已有 patch 的独立重放入口为 `runner.replay(spec, patch_path, new_directory)`；例如：

```bash
python -c 'import json; from pathlib import Path; from evaluation.linux_baseline.catalog import TASK_SPECS; from evaluation.linux_baseline.runner import replay; print(json.dumps(replay(TASK_SPECS[0], Path("evaluation/linux_baseline/results/p0-dc179b0/offline-T01.patch"), Path(".nz-coder-runs/p0-replay-new")), indent=2))'
```

该 offline patch 只新增 probe 文件，目标验收应继续失败；这不是可以通过 T01 的参考答案。

## 下一步与明确未验证项

唯一优先推进方向：**取得明确预算后，完成同一模型的费用接线冻结并执行 T01/T04 首次真实试运行**。
零真实样本不足以推荐新的 Agent 架构瓶颈；不预先决定做 RAG、长记忆或更多子 Agent。
真实错误、未知 usage、预算失控或基础设施阻断必须保留首个 attempt，停止后续请求；
修配置/驱动后启用新 experiment_id，不挑最好结果覆盖首次记录。

本轮未验证真实模型解题率、真实 token 效率、付费请求上限、完整交互 TUI、daemon/HTTP、Windows 或官方 SWE-bench。
此前 Windows HTTP 的 `test_http_abort_retires_stream_part_before_run_settles` 与
`test_http_run_settled_is_the_manager_commit_barrier` 开放失败继续保留，不因 Linux 离线检查而关闭。
没有跑无关全量 CI，没有修改 main、创建 PR 或合并。

## P1 续接：离线计费适配，真实运行仍关闭（2026-09-08）

本节追加于 P0，不替换 P0 的任务、验收或成绩。续接时 HEAD 为
`174dcb67728f60dbb535e9ee47775a700f2488a2`，中断留下的 P1 文件尚未提交。
本节不是已冻结的 live 实验，也不宣称真实预算闭环已通过验证。

### 授权、连接与待冻结项

没有获得明确付费授权。CNY 10 总额、每题 CNY 5 仍是建议，不是授权。
允许申请的范围仍为 T01 → T04 串行，各一次，不含独立付费探针或剩余十题。

通过产品的 `load_config_snapshot → active_model_selection → provider_connection`
进行只读检查：Provider 为 `openai-compatible`，model ID 为
`deepseek-v4-flash`，endpoint 为官方 `api.deepseek.com` HTTPS 根路径或 v1 路径，
但此次快照未解析到凭据（credential source 为 default），variant 为 None。
这只确认配置解析结果，不证明账户属于哪一计费合同、使用哪种币种或费率。
没有读取或打印凭据正文，没有调用账户 API 或付费健康探针。

现有适配候选仅支持官方 DeepSeek、CNY、thinking enabled / high；
这些限制不是对现有 effort 的继承证明，也不适用于第三方接口。
候选费率为每百万 Token 缓存命中 0.10、未命中 3、输出 9，候选来源为
`https://api-docs.deepseek.com/zh-cn/quick_start/pricing/`，代码日期为 2026-09-07。
**这些数值与日期来自中断实现，尚未核实当前页面及该账户的适用规则。**
时段规则、费率版本及账户币种必须在 live 前重新确认；不能把候选费率写成真实账单。

候选每请求预留：输入 1,048,576 Token、输出最多 8,000 Token。
按上述候选价格，预留为 `(1048576 × 3 + 8000 × 9) / 1000000 = 3.217728 CNY`，
预留 Token 为 1,056,576。输入预留意在覆盖完整模型输入上界，而不是用字符数除四估算；
这个模型上界本身仍需官方模型限制证据支持。
因此 P0 草案的 100,000 **累计用量**无法准入首请求。
中断实现使用每题 2,000,000 累计 Token 的候选值，尚未成为本次冻结配置；
不是宣称模型上下文窗口为二百万，也没有在运行中增加预算。
30 轮只限制主循环；600 秒为整个 worker 的墙钟时间，包含辅助调用，
补丁导出和独立验收在 worker 终止后执行，不包含在这 600 秒内。

### 已接入的离线可验证边界

- 保留正式 headless CLI → Native 链；仅在独立 worker 中替换 Provider 客户端工厂，未另写 Agent 循环。
- OpenAI SDK 下方的 HTTP transport 在每次发送前检查授权、任务/实验金额与累计 Token，
  先持久化预留和 dispatch intent，再允许 transport 执行。
- `max_tokens` 实际进入请求且不超过 8,000；拒绝同时携带 `max_completion_tokens` 的冲突请求。
- SDK 最多两次内部重试，每次重新经过 transport；底层 HTTP transport 重试为零。
  一旦已发送请求的费用不确定，后续 SDK 或 Agent 重试不得再次发送。
- 缓存命中/未命中拆分输入；reasoning 是输出子项，不重复加总。Decimal 按 1e-9 向上取整。
  `usage_derived_cost` 只是配置费率下的计算值，`provider_reported_cost` 保持 null。
- 缺失 usage、超时、取消、响应丢失、身份不符及结算落盘失败均保留不确定预留并阻断后续请求。
  汇总按 request ID 折叠事件，不重复统计 reserve/dispatch/settle；损坏日志令余额未知，不返还为全额。
- 兼容客户端及继承主客户端的 sidecar 共享 ledger；其他内置 Provider 的客户端创建被拒绝。
  子 Agent、handoff、动态工具、MCP、规划、反思、外部模型探测等不在本次受限工具/环境配置中。
  Bash 子进程沿用产品的凭据环境过滤；临时目录并非操作系统沙箱，不能据此承诺任意恶意命令的网络隔离。
- 实验目录、任务目录与 worker-started 标记拒绝重复启动；两题组织器遇到费用或基础设施不确定即停止。
  普通解题失败可以继续 T04，失败现场仍导出补丁并调用 P0 干净副本重放与独立验收。
  组织器在等待 worker 时被取消，也继续保存可获取的补丁与结果，清理状态保守记为未核实；
  独立验收进程启动失败、超时、缺少有效用例证据或缺失验收组，不会被当成普通题目失败而继续 T04。

请求日志只存用量、金额、状态与请求/系统/工具哈希，不保存 Authorization、key 或模型正文。
原始运行 JSONL 属于私有现场，不进入公开报告；未来真实结果仍需独立隐私审查。
dispatch 计数表示持久化发送意图，崩溃发生在写入意图与 socket 发送之间时可能高估，不能冒充精确账单请求数。

### 本次验证与真实成绩分离

复用中断前的 `.nz-coder-runs/p1-env`，Python 3.13.12、pytest 8.4.2、
openai 2.36.0、httpx 0.28.1；pytest 已符合项目 `>=7,<9`。
没有重建十二题或重跑整套 P0。受控 transport 经真实 SDK/Provider/Native 接线，
其脚本响应及 probe 写入不属于模型解题结果。

本次实际执行的最终定向命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py \
  tests/test_headless_cli.py --tb=short \
  --junitxml=.nz-coder-runs/p1-resume-20260908/contracts-reviewed.xml
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner --help
.nz-coder-runs/p1-env/bin/python -m ruff check \
  evaluation/linux_baseline/billing.py evaluation/linux_baseline/live.py \
  evaluation/linux_baseline/live_worker.py evaluation/linux_baseline/runner.py \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py \
  tests/evaluation/p1_controlled_worker.py --output-format concise
git diff --check
```

最终定向测试 **67 passed，57.05 秒，退出码 0**。帮助命令退出码 0。
对本轮七个 Python 文件运行上述 Ruff 检查通过，
`git diff --check` 通过；没有把这些结果计入 T01/T04 正式成绩。

`--live` 与 `--live-config` 已进入帮助并有拒绝无授权启动的测试；本次没有执行 live 命令，
也没有创建填入建议金额后标记 authorized=true 的配置。

本次修复先复现了冲突输出上限、其他内置 Provider 工厂未阻断、验收基础设施故障继续下一题、
空验收映射及组织器取消丢失收尾等失败，再做最小修正。
独立只读审查没有写代码；Token 跨题重置建议经对照“单题累计预算”的原要求未采纳，
快照字段改为 `per_task_cumulative_token_budget` 以消除歧义。

| 项目 | T01 | T04 |
| --- | --- | --- |
| 正式启动次数 | 0 | 0 |
| 实际模型请求/内部重试 | 0 / 0 | 0 / 0 |
| 真实独立验收 | 未运行 | 未运行 |
| 最终补丁及重放证据 | 无正式 attempt | 无正式 attempt |
| runtime_completed / patch_verified | null / null | null / null |
| within_budget / usage_complete / cleanup_ok | 不适用 | 不适用 |
| final_status | not_run | not_run |

本次新增真实调用支出、实际预留、不确定在途预留均为 0，依据是没有打开真实请求，
不是依据离线 usage 或余额变化。未获授权的任务/实验剩余额度为不适用；账户实际币种与费率仍未知。
十二题全部 not_run（含预选两题及其余十题），没有成功率样本。
没有观察到真实请求绕过预算，因为根本没有真实请求；离线契约通过不能替代此项真实证据。

下一步先补齐账户连接、适用费率证据、effort/Token 限制决定和明确授权，
再冻结已提交 Agent/驱动、任务/验收哈希、依赖及功能配置，才能创建新的 live experiment_id。
当前不运行剩余任务，不修改 main、不合并、不创建 PR、不 push。

## P1 实跑前免费核验（2026-09-08，续接 24093d0）

本节追加历史，不重做 P0。本轮交付为 **A：免费前置核验完成，缺明确授权和凭据，真实启动 0**。
本轮只调整费率查阅元数据及对应校验测试，没有重写计费系统、Agent、任务或验收。
保留原诊断压缩包及最终证据，不恢复旧测试展开目录，不继续磁盘清理。

### A. 正式连接解析

在目标 worktree 使用保留的 p1-env，经正式配置快照与 Provider 解析确认：
Provider `openai-compatible`，endpoint **`https://api.deepseek.com`**，model ID
`deepseek-v4-flash`，variant 为 None，**凭据缺失，来源类别 default**。
没有从其他 worktree、账户或旧日志取凭据，没有输出完整配置或凭据。
配置检查不是认证成功或服务可用证明，本轮没有做模型请求、账户查询或付费探针。

### B. 官方资料、版本与费用口径

通过不携带凭据的普通 HTTPS GET 阅读以下官方页面；查阅批次开始于
**2026-09-08 06:47:24 UTC（北京时间 14:47:24）**。
原始页面保存在私有 `.nz-coder-runs/p1-preflight-20260908/sources/`，本节保存可公开摘录及哈希。
这不是 Parallel 搜索服务或其他付费模型调用。

| 官方来源 | 支持本轮结论的内容 |
| --- | --- |
| [模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) | `deepseek-v4-flash`；模型版本 `DeepSeek-V4-Flash-0731`；OpenAI 格式 base URL `https://api.deepseek.com`；上下文 `1M`，最大输出 `384K`。价格单位为百万 Token，金额为人民币元。 |
| [思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) | “思考模式默认打开，且 effort 默认为 high”；OpenAI 格式使用 `thinking: {type: enabled}` 与 `reasoning_effort: high`；携带 tools 的后续请求须回传 reasoning_content。 |
| [Chat Completions API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion) | “输入 token 和输出 token 的总长度受模型的上下文长度的限制”；`max_tokens` 限制生成量；prompt 等于 cache hit + cache miss，total 等于 prompt + completion；reasoning 为 completion 细项。 |

页面内容 SHA-256：

- pricing.html：`899affbdbc33d0be620d8dea59e86f5036c11b5410b14d060b8d2874c74f38e5`
- thinking.html：`20e5a177c46617794a070a1c83aa5635bfc73e2d91c93a02f27ad0d5d0c8ce83`
- chat-completion.html：`25c5aea0a51fc349804ab596a92c5e57afa7bccd5d2dab5d067d60d3805f62cb`

官方 Flash 标准公开价：

| 每百万 Token，CNY | 输入缓存命中 | 输入缓存未命中 | 输出（含 reasoning） |
| --- | ---: | ---: | ---: |
| 高峰时段 | 0.10 | 3.0 | 9.0 |
| 空闲时段 | 0.05 | 1.5 | 4.5 |

原文：“高峰时段为北京时间周一至周五 9:00 - 12:00、14:00 - 18:00（其余为空闲时段）”。
充值余额和赠送余额同时存在时优先扣赠送余额；价格页保留调价权。
本轮不根据本机时钟擅自套用半价：**预留和 usage 计算统一使用已核实的高峰价格上界**，
因此空闲时段的计算值可能高于最终账单，但不会误用最低价放大可用预算。
`usage_derived_cost` 是该明确口径下的费用上界；`provider_reported_cost` 仍为 null。
不声称已核对任何账户内部合同、赠送额度或账单；当前未有用户说明特殊合同/转发计费，
若账户实际不适用标准官方价格，则本次预览不能替代该账户费率。

官方上述页面未提供这张价表的明确**生效日期**，保持 null；不能把查阅日期伪装成生效日期。
配置兼容字段 `rate_date` 现在明确表示本次来源的查阅日期 `2026-09-08`，
冻结元数据分别保存 `retrieved_at`、`effective_date`、来源哈希和高峰价计算口径。
旧候选日期 `2026-09-07` 不再被误当成已核实来源，授权/官方 endpoint/价格下限检查仍保留。
官方版本名称只记录查阅时信息，请求仍使用用户指定的 model ID，不静默换模型。

### C. 待用户确认的最终候选与免费预览

| 配置 | 候选值与含义 |
| --- | --- |
| 模型与思考 | 官方 `deepseek-v4-flash`，thinking enabled、high；与官方默认一致，但本轮仍待用户确认 |
| 单请求输出 | `max_tokens <= 8000`，包括生成的 reasoning，低于官方最大输出，不等于 384K 模型上限 |
| 输入保守预留 | 1,048,576 Token；以公开 1M 窗口作全输入上界，并取不小于十进制 1M 的二进制换算值；不是实际输入量 |
| 共享窗口 | 输入输出共享官方 1M 上下文；各自最大预留相加只是保守矩形上界，不声称两者能同时达到该值 |
| 单题累计 Token | 2,000,000，跨本题主/辅助请求累计；不是上下文窗口，也不是两题共同的 Token 上限 |
| 主循环 | 最多 30 轮，不是最多 30 次 HTTP 请求 |
| worker 墙钟 | 600 秒，涵盖主/辅助模型与 worker 内收尾；终止后的补丁导出和独立验收另计 |
| 工具 | list_directory、read_file、write_file、edit_file、apply_patch、bash、glob_search、grep_search、repo_map、read_symbol、find_symbol_callers、update_scratchpad、read_scratchpad、todo、diff_status、verify_changed_files |
| 辅助配置 | planning/reflection/MCP/child agents/handoffs/dynamic tools 关闭；保留的 sidecar 继承主客户端并共享 ledger；其他内置 Provider 创建被阻断 |
| 重试 | SDK 最多 2 次自动重试、底层 transport 0 次重试；任何已发出请求用量不确定后禁止再次发送 |
| 申请预算/范围 | 总额 10 CNY、每题 5 CNY；仅 T01 → T04 串行，每题一次；两题结束停止，不含付费探针及其余十题 |

首请求免费算术预览（没有 reserve/live 调用，更没有写入真实授权配置）：

`(1048576 × 3 + 8000 × 9) / 1000000 = 3.217728 CNY`；Token 预留为 **1,056,576**。
建议总额/单题额在预留后分别余 **6.782272 / 1.782272 CNY**，单题 Token 余 **943,424**，
因此首请求可以准入建议额度。但这些不是已授权余额，也不是已实际发生的预留或花费。
完整合法 usage 持久化后，用 cache hit、cache miss、completion 各按对应高峰费率计算，
金额按 1e-9 CNY 向上取整，释放预留与结算额之差及多余 Token；子项不重复加总。
缺 usage、失联、取消或结算持久化失败保留不确定预留，停止后续请求。
下一请求必须同时满足总金额、单题金额及单题累计 Token 余额足以预留，且仍在时限内、未阻断。
固定全窗口预留可能在尚有余额时提前停止；不提高授权额、不改 tokenizer 来规避。

### 最终离线验证与交付

复用 p1-env：Python 3.13.12，pytest 8.4.2（符合 `>=7,<9`），openai 2.36.0，httpx 0.28.1，
rich 14.2.0、prompt_toolkit 3.0.52、PyYAML 6.0.3、tree-sitter 0.26.0、watchfiles 1.1.1。
本轮为 freeze 补录实际 HTTP 客户端版本，没有安装或替换环境。
manifest 哈希仍为 `e6dbaf4407e96ec4c814951028d265588f027d796fe313b57adcae88a247ae89`。
实际执行命令（JUnit 是本次免费核验输出，不是正式实验目录）：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py \
  tests/test_headless_cli.py --tb=short \
  --junitxml=.nz-coder-runs/p1-preflight-20260908/contracts.xml
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner --help
.nz-coder-runs/p1-env/bin/python -m ruff check \
  evaluation/linux_baseline/live.py tests/evaluation/test_p1_live.py --output-format concise
.nz-coder-runs/p1-env/bin/python -m py_compile \
  evaluation/linux_baseline/live.py tests/evaluation/test_p1_live.py
git diff --check
```

本轮最终结果：**71 passed，56.47 秒，退出码 0**；帮助、Ruff、编译检查与 `git diff --check` 均通过。
新增测试只覆盖查阅/生效日期区分、旧候选来源拒绝及全时段预留不能被半价配置替代，
原有受控 transport、SDK 重试、辅助调用、重复 attempt 与两题停止契约继续通过。

| 任务 | 正式启动 | 模型请求/SDK重试 | Agent终态 | 目标验收 | 原有回归 | 补丁重放 | 输入/输出/缓存Token | 计算费用 | 未知预留 | 预算状态 | 原因 |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | 0 | 0 / 0 | not_run | 未运行 | 未运行 | 无正式补丁 | null | 不适用 | 0 | 未授权 | 授权及凭据缺失 |
| T04 | 0 | 0 / 0 | not_run | 未运行 | 未运行 | 无正式补丁 | null | 不适用 | 0 | 未授权 | 授权及凭据缺失 |

本轮实际新增模型费用、实际预留和不确定在途费用均为 **0**，依据是没有发送真实模型请求，
不是把合成 usage 当账单。两题的 runtime_completed、patch_verified、within_budget、usage_complete、cleanup_ok
均未产生真实结果，保持 null；真实成功率为 null。其余十题仍为 not_run，十二题均未启动。
离线通过不代表真实费用控制、TUI、Windows HTTP、多 Agent 产品或 SWE-bench 已验证。

一次性待确认：上述官方模型、high 与运行限制、总额 10 CNY/每题 5 CNY、仅两题一次的付费范围；
并在本机正式配置中提供可解析的官方账户凭据（不要发送到对话）。账户若有特殊计费约定需说明。
取得明确消息授权后才能写 authorized=true 及真实 authorization_reference；
本轮没有创建真实授权文件、experiment_id 或 live 输出目录，没有调用 live.run 作预览。
本轮改动测试后提交，未来 harness_revision 使用该提交后的实际 HEAD，保持源码干净再运行；
不修改 main、不创建 PR、不合并。沿用本会话已有 push 授权推送本轮相关提交后结束，不再追加准备工作。
