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

## P1 首次真实运行：T01 补丁通过，辅助请求计费未知后停止（2026-09-08）

本轮交付 **B：已运行 T01 首次 attempt，计费不确定触发停止，T04 未运行**。
不是 2/2 完成，也不是十二题或 SWE-bench 成绩。以下是新的真实结果，不替换此前 offline/reference 记录。

### 授权、冻结与实际启动

用户于 2026-09-08 07:19:18.189 UTC 明确确认付费授权，07:30:07.808 UTC 确认读取主项目 `.env`。
授权为官方 DeepSeek `deepseek-v4-flash`、thinking enabled/high，总额 10 CNY、每题 5 CNY，
仅 T01 → T04 串行各一次，结束即停；没有独立付费探针。
只读取已明确指定文件中的必要连接字段，核对 Provider/model/endpoint/effort 后注入隔离进程环境，
没有复制整份 `.env`、凭据文件、历史记忆或开发者环境。

正式实验为 **`p1-live-20260908-073600`**，Agent revision
`7c308e3a75deae112e20c0de225113fda6ec9f9e`，harness revision
**`0e1560c3947941005e68d834f377d4468418c07a`**。
任务 manifest 哈希仍为 `e6dbaf4407e96ec4c814951028d265588f027d796fe313b57adcae88a247ae89`。
运行期间没有修改 Agent、驱动、任务、验收或费率，仍使用本节之前已冻结的工具范围、依赖和限额。
具体配置及各验收哈希见 [真实结果安全投影](../../evaluation/linux_baseline/results/p1-live-20260908-073600/result.json)。

保留一次**出网前启动配置失败**：候选 `p1-live-20260908-073120` 的组织器 HOME 被放在驱动 workspace 内，
触发 `ConfigValidationError: Workspace trust store must be outside the workspace`。
它在 `credential()` 阶段退出，正式实验目录未创建、任务启动 0、模型请求 0、费用 0。
修正仅为将组织器 HOME 放到 workspace 外的新隔离临时目录，未改源码；旧候选授权文件和失败记录保留，
使用上面的新实验身份。它不是 T01 的第二次解题 attempt。

启动前定向测试 **71 passed，55.28 秒，退出码 0**，JUnit 保存在
`.nz-coder-runs/p1-preflight-20260908/pre-live-contracts.xml`。
通过只保留必要环境变量的启动器，实际执行以下正式 CLI 参数（不是手工调用模型）：

```bash
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner \
  --live \
  --live-config "$PWD/.nz-coder-runs/p1-live-20260908-073600.config.json" \
  --output "$PWD/.nz-coder-runs/p1-live-20260908-073600"
```

其中授权文件不含 key；密钥只通过经核对的进程环境注入。未使用 `--dry-run` 或离线 `--publish`。
原始私有现场为 `.nz-coder-runs/p1-live-20260908-073600/`，组织器最终退出码 **1**，`stopped=true`。

### 两题结果与独立验收

| 任务 | 正式启动 | 请求及 SDK 重试 | Agent 终态 | 目标验收 | 原有回归 | 最终补丁重放 | 输入/输出/缓存命中 Token | 按费率计算费用 | 未知预留 | 预算状态 | 最终状态/原因 |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | 1 | 13 个已结算响应 + 1 个未知 dispatch；SDK 重试发送 0 | completed，进程退出 0，37.9977 秒 | 3/3 通过 | 4/4 通过 | 通过，范围合规 | 已知部分 120446 / 2657 / 59904 | 0.211529400 CNY（已知部分） | 3.154944000 CNY | 账本 within_budget=true，用量不完整 | infrastructure_blocked：辅助验证器请求 14 计费未知 |
| T04 | 0 | 0 / 0 | not_run | 未运行 | 未运行 | 无正式补丁 | null | 不适用 | 0 | 未消耗单题额度 | not_run：按停止规则未启动 |

T01 分项：`runtime_completed=true`、`patch_verified=true`、`within_budget=true`、
`usage_complete=false`、`cleanup_ok=true`、`final_status=infrastructure_blocked`。
主运行完成和补丁通过均是真实事实，但不能据此覆盖计费未知并记作正常端到端成功。
T04 对应运行/验收字段未产生结果，不用 false 或零用量冒充实跑。

T01 最终修改 `textkit/parser.py`，补充空白输入处理，并在 `tests/test_public.py` 增加开发测试。
组织器没有修改正式副本帮助解题。独立回归使用冻结的原始四个检查，不以 Agent 修改后的 public tests 替代。
模型未收到独立隐藏验收反馈，也没有参考修复或 Fake 成绩填补。

公开 [T01 最终补丁](../../evaluation/linux_baseline/results/p1-live-20260908-073600/T01.patch)
为原始导出文件的逐字副本，1193 字节，SHA-256：
`e4619db21366552daca85d888128a50de788e49d5d963d426655a65086eae0f3`。
原始 `T01/replay/repo` 是由 P0 materialize 创建的干净初始副本，已应用该补丁；
`T01/replay/evaluator/target.xml` 和 `regression.xml` 分别记录上述 3/3 与 4/4 通过，无跳过、收集错误或超时。
失败现场、计费日志、原始 Session 和补丁均未清理或回退。
报告与 JSON 投影通过差异空白检查；归档 patch 的两个空白上下文行保留标准 diff 前缀空格，
不为消除补丁文件本身的 trailing-whitespace 提示而改变原始字节，已用哈希及逐字比较验证一致。

### 费用、未知项与停止证据

已知 13 个响应累计输入 **120,446** Token，其中缓存命中 **59,904**、未命中 **60,542**；
输出 **2,657**（reasoning 包含在输出内），合计 **123,103** Token。
按冻结高峰费率独立复算：
`(59904 × 0.10 + 60542 × 3 + 2657 × 9) / 1000000 = 0.2115294 CNY`。
这只是已知 usage 的计算值，不是完整实际账单；`provider_reported_cost=null`。

请求 14 的输出预留为 1,024 Token，其金额预留为
`(1048576 × 3 + 1024 × 9) / 1000000 = 3.154944 CNY`。
该请求日志依次为 reserved → dispatching → uncertain，无有效结算记录；全额预留继续保留，
没有视为免费或退款。当前总额账本可用 **6.633526600 CNY**、T01 可用 **1.633526600 CNY**；
T01 剩余累计 Token **827,297**，已经扣除已知用量和未知 Token 预留。
费用总额/完整用量依然 **unknown**，不能因已知费用小而宣称真实预算闭环完整验收。

`reserved_cost=44.985408000 CNY` 是十四次请求预留的**累计周转量**，不是同时占用金额，更不是实际支出。
按事件顺序复算，已知费用加当时未结算预留的最高占用为 **3.408343800 CNY**，低于单题 5 CNY。
所有已记录 dispatch 前都有预留；账本未记录任何绕过预算的发送，未知后没有新的 dispatch，T04 目录未创建。
SDK 重试**发送**计数为 0，不据此宣称 SDK 没有尝试过被预算边界拒绝的逻辑重试。
14 是持久化 dispatch intent 计数，13 个已知响应可以确认完成，另 1 个不能当作已核实账单请求。

请求 14 的 system/tools SHA-256 与代码中的 **sidecar verifier** 完全匹配，已确定这是保留的辅助验证调用，
且它经过同一个预算 transport，不是遗漏的未计费路径。
发现一个应优先离线核实的具体请求语义偏差：Agent 的 `_verifier_capability_options()` 对 DeepSeek V4
显式设置 thinking disabled，而 P1 `BudgetTransport` 会统一改回 enabled/high；该调用同时使用强制 verdict tool。
本轮日志未保存导致 uncertain 的具体 HTTP 状态、响应或异常类别，**不能据此断言服务端返回了哪一种错误**，
也不能把上述语义偏差未经复现就写成已证实的唯一根因。

账本 SHA-256：`c90dad4b6c3503d1aba3a08d4b883c48ced382ca4e22ba8985cdcc425f4c626a`；
冻结文件 SHA-256：`43ffb167a0f9f7db3d4b666bbcaa0c8ba12b06a4dfef9532cdfa84cb93dd63f8`。
运行目录扫描未发现实际凭据值，公开投影不含 key、授权正文、模型正文或宿主绝对路径。
进程清理记录正常，结束后没有发现本次 runner/live_worker 进程遗留。

### 本轮停止与后续边界

本轮已停止，不自动重试 T01，不启动 T04 或其余十题。合计已启动 **1/12**，未运行 **11/12**。
这一个本地 fixture 的补丁验收通过，不代表 Linux TUI、Windows HTTP、完整多 Agent 产品或 SWE-bench 得分。
下一步应先离线修正/验证辅助验证器请求模式与计费错误诊断，而不是运行剩余题目；
后续修改必须使用新的实验身份，保留本次首次结果及未知预留，不覆盖成绩或擅自再次花费。
本报告及安全投影是在实验停止后追加，提交不改变本实验冻结的 harness revision。

## 🔧 P1 辅助请求模式与失败诊断离线修复（2026-09-08）

本节只记录 `c6108e6b33b729eb4a3137cd1b58b30fd5aab599` 之后的离线修复，不改写以上历史。
本轮正式任务新增启动 **0**、真实模型新增调用 **0**、新增真实费用 **0 CNY**。
没有读取真实凭据、调用账户接口、发付费探针、重跑 T01 或启动 T04；也没有创建新的真实授权文件。
实现限定在 P1 harness 与测试，`git diff 7c308e3a75deae112e20c0de225113fda6ec9f9e -- nz_coder`
无差异，Agent revision 仍为该 SHA，任务与独立验收未变。

### 已证实的缺陷与参数责任

修复前新增测试从真实 `invoke_sidecar_verifier()` 起，经正式 Gateway、Provider、OpenAI SDK
到受控 inner transport，实际捕获 `thinking={"type":"enabled"}`，而产品显式要求 `disabled`。
失败断言是请求模式不一致，不是导入、环境或认证错误。

| 边界 | sidecar 的实际要求或行为 | 本轮处理 |
| --- | --- | --- |
| `sidecar_verifier._verifier_capability_options` | DeepSeek V4 显式 disabled | 产品源码保持不变 |
| `invoke_sidecar_verifier` → `ModelCall` | VERIFIER、强制 `emit_sidecar_verdict`、1024 输出 | 产品源码保持不变 |
| `ProductionModelGateway._request_kwargs` | 传递工具、输出限制及 capability options | 产品源码保持不变 |
| `OpenAICompatibleProvider.create_completion` → SDK | capability 规范化、extra_body 序列化 | 仅 P1 进程内 seam 补缺省模式 |
| `BudgetTransport` → inner transport | 旧版无条件覆盖 enabled/high | 改为验证允许模式集合，保留业务参数 |

正式 Provider 使用 capability/variant request options；无 variant 时不会补 DeepSeek thinking。
P1 的 `bounded_product` 在 Provider 构造边界把缺省主模式显式确定为 enabled/high，
与官方文档的默认值一致[^p1-mode]。调用方已有字段不被这层替换；最终 Provider/SDK 请求仍由 transport 检查。
sidecar 显式 disabled 保留且不注入 effort。`messages`、`tools`、`tool_choice`、`response_format`
不因计费修改；输出上限允许在 transport 按冻结限额收紧，但不会将 sidecar 1024 抬到 8000。

新增配置明确分开以下两种模式；`effort` 字段只描述主模式，不再代表所有请求：

| 模式 | thinking / effort | 实际输出上限 | 预算边界 |
| --- | --- | ---: | --- |
| 主编码 | enabled / high | 8000 | 原任务与实验账本 |
| 允许的辅助模式 | disabled / 不适用，省略 effort | 1024 | 同一账本、Token累计及未知停止规则 |

新增 `main_thinking`、`main_output_limit`、`auxiliary_thinking`、`auxiliary_output_limit` 配置项，
freeze 保存 `request_modes`。旧配置缺这些字段时不能作为新模式授权启动；旧授权文件不迁移、不改写。
[未授权示例](../../evaluation/linux_baseline/p1-unauthorized.example.json) 明确 `authorized=false`、额度为 0、
无授权引用且未冻结，不是可运行的真实 grant。

计费边界没有跨 Gateway 工作线程可信地接收到 `ModelCallPurpose`，因此逐请求用途如实记为 `unknown`。
它验证冻结的允许模式集合，而非按“主/辅助”文本标签授予权限；system/tools 哈希仅用于审计。
任意提示词、工具名称都不能放行未授权模型、endpoint、effort 或其他模式。
本轮 disabled 请求若仍携带 reasoning_effort，按受限配置冲突发送前拒绝，不静默删除或替换。
这是本试运行的明确限制，**不是宣称官方接口一定拒绝 disabled 与 effort 同时出现**。

官方资料仅使用无需凭据的公开 HTTPS GET，完成查阅时间为 `2026-09-08T08:34:40Z`：
思考指南确认 enabled/disabled、默认 enabled/high；Chat API 文档确认强制函数工具语法、
max_tokens 为 completion 上限且输入输出共享上下文[^p1-mode][^p1-chat]。
公开页面未给出能证明历史请求 14 具体错误的证据，不把两份参数文档拼成“确定 HTTP 400”。
原始页面保存在私有修复证据目录 `sources/`，SHA-256：

- `thinking.html`：`20e5a177c46617794a070a1c83aa5635bfc73e2d91c93a02f27ad0d5d0c8ce83`
- `chat.html`：`25c5aea0a51fc349804ab596a92c5e57afa7bccd5d2dab5d067d60d3805f62cb`

查阅日期不是费率生效日；本轮没有改变原费率、输入预留 1,048,576、累计 Token 2,000,000、
主循环 30 轮或 worker 600 秒。固定全窗口预留仍然保守，预留不是实际花费。

### 安全诊断、结算与停止

复用 `billing.jsonl`，每个 HTTP 边界入口使用既有 request_id 序列，新增安全事实和 diagnostic，
没有另建事件数据库。request_facts 记录实际 requested/effective thinking、effort 及是否适用、
tool_choice 类型、有效输出上限、SDK retry index 与审计哈希。
diagnostic 记录阶段、确实收到的 HTTP status、固定异常类别、usage_present、结算状态、
evidence_saved 和次要失败。没有保存原始异常字符串、动态类名、服务端 error.message/error.code、
正文、凭据、Authorization 或带查询参数 URL。读取响应前 usage_present 为 null，不伪造 usage=0。

| 故障阶段 | 诊断行为 | 费用与后续请求 |
| --- | --- | --- |
| 配置、authorization、admission、reservation | rejected，无 inner 发送，无 Provider usage | 未发生的预留为 0，拒绝原因留存 |
| reservation/dispatch persistence | 区分预留写入和发送意图写入 | inner 未发送；已有保守预留不因写失败消失 |
| transport_send / http_response | 区分发送异常与实际收到的 HTTP 状态 | 不确定请求保留预留，SDK逻辑重试不再发送 |
| response_read / json_parse | 区分读取超时、丢失与 JSON 损坏 | 不确定、停止 |
| response_identity / usage_validation | 区分身份与用量缺失、不一致、越界 | 不确定、停止 |
| settlement_persistence | usage 读取成功仍须可靠落盘后才能释放 | 写失败保留未知预留 |
| response_close / transport_close / diagnostic persistence | 不覆盖第一故障；次要错误也脱敏 | 阻断保持；取消继续传播，双重取消保留首次取消 |

合法 usage 加无效 verdict 是两个维度：费用可以已知，`accept` 也可能是
`no_tool_call`、`invalid_verdict_value`、`provider_error` 或 `timeout` 的产品降级结果。
本轮不改变 verifier fail-open 策略；只有测试返回的合法精确工具判定才记为 `verifier_ok`。
judge 超时后晚到的可靠 usage 可以结算，但不会把已经超时的语义判定改写成通过。

追加诊断失败时，进程内仍保存第一阶段/类别、阻断状态与必要预留；取消不转为普通成功。
还复现并修复了“settled 行已写出但 fsync 报错，后续 uncertain 写失败”的组织器边界：
父进程现在要求 worker 最终摘要存在且与 journal 重建结果一致。
缺失、损坏或不一致时保留原文件及 journal 视图，余额标为 unknown，仍导出/重放补丁，但不启动下一题。
worker 最终摘要写入失败也不会覆盖已有的主错误或取消。
旧日志缺少新 diagnostic 时返回 `historical_not_recorded` 与空状态，不推导旧 HTTP 原因。

### 离线复现与回归证据

复用 p1-env，Python 3.13.12、pytest 8.4.2、OpenAI SDK 2.36.0、httpx 0.28.1；未安装依赖。
证据目录为 `.nz-coder-runs/p1-sidecar-repair-20260908/`，与真实实验分离，不使用 live publish。
新增集成测试为 [test_p1_sidecar.py](../../tests/evaluation/test_p1_sidecar.py)，
子进程复用 [p1_controlled_worker.py](../../tests/evaluation/p1_controlled_worker.py)。
评测测试与受控子进程禁止外部 socket，使用虚假 key，不加载真实授权文件。

修复前实际执行（当时为原 transport，仅新加 sidecar 复现测试）：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_sidecar.py --tb=short \
  --junitxml=.nz-coder-runs/p1-sidecar-repair-20260908/red-mode.xml
```

结果：`1 failed in 1.34s`，明确断言 `{'type': 'enabled'} != {'type': 'disabled'}`。
修复模式后的实际命令与结果：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_sidecar.py --tb=short \
  --junitxml=.nz-coder-runs/p1-sidecar-repair-20260908/green-mode.xml
```

结果为 `7 passed in 10.56s`（此后继续添加了诊断及语义/费用分离测试）。
后续诊断、冻结配置、落盘及取消的 red/green JUnit 也保存在该私有目录，
其中 `red-review.xml` 为 5 个确实失败的边界测试，修复后 `green-review.xml` 为 12 项通过；
`red-first-cancellation.xml` 为 2 个首次取消被覆盖的失败，`green-first-cancellation.xml` 为 6 项通过。

最终完整定向命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py \
  tests/evaluation/test_p1_sidecar.py tests/test_headless_cli.py --tb=short \
  --junitxml=.nz-coder-runs/p1-sidecar-repair-20260908/contracts-verified.xml
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/test_sidecar_verifier.py tests/test_llm_judge.py \
  tests/runtime/model_gateway/test_buffered_gateway.py tests/runtime/model_gateway/test_runtime.py \
  tests/runtime/model_gateway/test_gateway_models.py tests/runtime/model_gateway/test_usage.py --tb=short \
  --junitxml=.nz-coder-runs/p1-sidecar-repair-20260908/product-regressions.xml
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner --help
.nz-coder-runs/p1-env/bin/python -m ruff check \
  evaluation/linux_baseline/billing.py evaluation/linux_baseline/live.py evaluation/linux_baseline/live_worker.py \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py tests/evaluation/test_p1_sidecar.py \
  tests/evaluation/p1_controlled_worker.py tests/evaluation/conftest.py --output-format concise
.nz-coder-runs/p1-env/bin/python -m py_compile \
  evaluation/linux_baseline/billing.py evaluation/linux_baseline/live.py evaluation/linux_baseline/live_worker.py \
  tests/evaluation/test_p1_billing.py tests/evaluation/test_p1_live.py tests/evaluation/test_p1_sidecar.py \
  tests/evaluation/p1_controlled_worker.py tests/evaluation/conftest.py
git diff --check
```

最终主套件为 `115 passed in 129.31s`；相关产品回归为 `112 passed in 1.58s`，共 227 项通过。
Ruff、编译、帮助及差异空白检查通过。
项目没有配置独立类型检查器，本轮未引入新的类型工具或依赖。

受控请求的可审查事实：主 Gateway 请求仍为 enabled/high/8000，消息、工具及 response_format 不变；
真实 sidecar 链为 disabled/effort省略/1024，强制工具未变，合法 verdict 得到 `verifier_ok`。
合成 usage 的独立预期费用为 `(40×0.10 + 60×3 + 20×9)/1000000 = 0.000364 CNY`，
只用于契约验证，不是新增真实支出。主/辅助两次响应共用账本结算 `0.000728 CNY` 的合成值；
辅助用量未知时只保留主请求已知部分，并持有 `3.154944 CNY` 合成预留，inner 总发送数保持 2。
SDK retry entries 与实际进入 inner 的发送分别验证，未知后的重试发送为 0。

组织器集成测试使用临时 fixture、真实 headless CLI/Native、真实工具修改及独立干净重放：
主运行 completed、目标 3/3、冻结回归 4/4、patch_verified=true；
随后真实 sidecar 调用链返回缺 usage 的受控响应，产品 verdict 为降级 `accept/provider_error`，
最终仍为 infrastructure_blocked，T04 启动 0。不是手写未知账本来替代这条链，也不是新的模型成绩。
另外覆盖未授权/额度不足零发送、别名输出限制拒绝、缓存/推理子项不重复、
其他模型/Provider拒绝、重复attempt、非200、发送/读取超时、丢失响应、坏JSON、身份错误、
缺失/矛盾/越界usage、记录失败、敏感哨兵与动态异常类名脱敏。
只读代理独立复核了模式、共享账本、首因/取消、脱敏及组织器停止边界；代码仅由主代理修改。

### 历史不变与下一次真实运行的边界

本轮前后复核以下 SHA-256 一致：

| 原实验文件 | SHA-256 |
| --- | --- |
| frozen.json | `43ffb167a0f9f7db3d4b666bbcaa0c8ba12b06a4dfef9532cdfa84cb93dd63f8` |
| T01/billing.jsonl | `c90dad4b6c3503d1aba3a08d4b883c48ced382ca4e22ba8985cdcc425f4c626a` |
| T01/final.patch（含公开副本） | `e4619db21366552daca85d888128a50de788e49d5d963d426655a65086eae0f3` |
| T01/result.json | `0d3c6436feb0e31568a19cf265d64159a417e5f1951729f7cb9a47df0548e5e8` |
| summary.json | `b67a7a99540c681197cc7efe5c85b119ebb2464385527ebd018acc11ea4c60a1` |
| 公开 result.json | `04cca793c45f5e766477074e3a79b96e24bc6a251802e5a3b1a73e1cbcd1b6b1` |

原授权配置未修改，交付核对 SHA-256 为
`22806e70391f9304c4aa01cdafa0e455549bbc5d3365685f1541abfe224c7b93`。
保留全部历史现场及压缩包，不解压、不清理。T01 历史正式启动仍为 1、T04 为 0；
历史已知折算费用仍为 **0.2115294 CNY**、未知预留仍为 **3.154944 CNY**。
历史账本总剩余 **6.6335266 CNY**，不是已核实账户余额；未知项未清零、未回填免费。
第14次请求具体 HTTP 状态、原始异常类别、完整用量与真实账单依然未知。

下一次真实运行前仍须明确主/辅助新配置及授权范围、使用新的冻结 harness/experiment 身份，
并明确沿用扣除未知预留后的余额还是另获新授权，记录与旧实验的关联。
新 experiment_id 不自动获得新的 10 CNY；不覆盖首次结果，不借本轮修复重跑 T01 或启动 T04。
本轮到离线修复及证据交付即停止，不申请预算，不自动恢复实跑。
结论是“请求模式改写缺陷已离线修复并验证”，不是“历史请求14的HTTP原因或账单已确认”，
更不是“T04通过”或“真实P1完成”。

[^p1-mode]: DeepSeek，思考模式指南，查阅于 2026-09-08T08:34:40Z。https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
[^p1-chat]: DeepSeek，Chat Completion API 参数文档，查阅于 2026-09-08T08:34:40Z。https://api-docs.deepseek.com/zh-cn/api/create-chat-completion

## P1：仅 T04 续接准备（2026-09-08，未开启新付费运行）

本节追加于修复基线 `ba1ab3960ea04e0f16761a94d3681082ee9c0521`，不改写历史实验。
交付状态为 **A：单题接线、预算承接及离线验证；等待本次明确续接确认**。
原 10 CNY 授权存在，但本次“只运行 T04、承接 5 CNY、明确辅助 disabled”方案仍待确认。
本轮没有创建真实 authorized=true 文件、真实 experiment_id、输出目录或续接登记，
没有真实模型调用、付费探针或账户查询，也没有读取/打印凭据。
示例配置保持 `authorized=false`、金额为 0；示例不是授权。

### 修改范围与执行边界

`live.execution_plan()` 只支持原 `[T01,T04]`，或带固定前序实验引用的 `[T04]`；
空列表、重复、额外任务、顺序不一致及混入 T01 的续接均拒绝。
同一执行计划进入授权校验、freeze、组织器、descriptor、worker 和 CLI 汇总。
worker 校验实验路径、任务身份、原始需求、Session、配置/冻结哈希及可用预算；
单 T04 grant 下的 T01 descriptor 在创建账本/客户端前拒绝。
CLI 只判断所选非空计划：T04 正常成功返回 0，失败、未运行、计费/基础设施阻断返回非零。
未选 T01 保持本实验 not_run，不伪造成功；原 pair 的第二题仍须校验第一题账本、摘要与串行剩余额度。

新增小型 `continuation.py`，仅处理原 `p1-live-20260908-073600` 的首次 T04 分配。
它以封存 SHA-256 校验原授权、freeze、T01 账本/补丁/结果及 summary，
重建账本金额，确认旧实验已停止、T04 未启动，沿用原 endpoint/model/effort/Token/费率。
领取额度前检查活动 baseline 进程和私有根目录下其他实验/支出痕迹；缺失或不一致时停止。
固定私有登记位置是 `.nz-coder-runs/p1-continuations/p1-live-20260908-073600/T04/`，
与新 experiment_id 无关。独占目录与 receipt 绑定新 grant hash、输出目录和承接信息，
并发/改名重启不能再次分配。

receipt 及目录完成 fsync 后才创建 `ready`；写入/同步失败会留下不可自动释放的占位，
没有 ready 的 receipt 不能启动 worker。ready 若在崩溃中丢失也按阻断处理，需人工核对。
“分配登记”与任务目录中的 `worker-started` 分开，前者不等于正式模型请求或任务已经执行。
失败后不删除登记、不自动换实验名重试。金额必须为规范 Decimal 字符串，
数字/bool/带空白或符号等格式在 freeze/登记前拒绝，避免子进程规范化造成 grant hash 变化。

生产 `nz_coder/`、`billing.py`、任务 catalogue/manifest、独立验收及旧公开结果均无改动。
Agent revision 仍为 `7c308e3a75deae112e20c0de225113fda6ec9f9e`。
本节所在提交是离线就绪版本；授权后使用当时干净 HEAD 作为新 harness_revision，
再冻结任务/验收哈希、工具范围、依赖和唯一输出目录，不把旧 T01 与新 T04 混成同一驱动成绩。

### 承接金额与待确认运行配置

只读核算：`10 - 0.2115294 - 3.154944 = 6.6335266 CNY`。
拟分配的 5 CNY 来自该余额，不是新增预算；未分配的 `1.6335266 CNY` 不授权继续使用。
新 ledger 只统计本次 T04 请求；旧请求14不伪造成新请求，不释放、不回填免费。
新 freeze 保存前序 ID/授权引用、证据哈希、旧 known/unknown、承接前余额、分配额及新授权/实验身份。
总约束为 `旧 known + 旧 unknown + 新 known + 新 unknown ≤ 10 CNY`，新部分另受 5 CNY 上限。
所有余额都是账本余额，不是已独立核实的账户余额；整个历史 usage 仍不完整。

候选仍为官方 `https://api.deepseek.com` 的 `deepseek-v4-flash`。
主编码 enabled/high/8000；辅助验证 disabled/effort 省略/1024，保留强制工具约束。
二者走原共享 transport/ledger 和未知请求停止规则，不关闭 sidecar、不改变产品门控。
单题累计请求 Token 2,000,000、主循环 30 轮、整个 worker 600 秒，均沿用原设置。
工具白名单和辅助功能限制不变；这不是完整多 Agent 产品或 SWE-bench 验证。

沿用上节 2026-09-08 查阅的官方峰值费率证据：每百万 Token，缓存命中 0.10、
未命中 3、输出 9 CNY。查阅时间不代表费率生效日期；不查询账户合同/账单，也不声称认证已再次验证。
本轮不修改已有保守预留：输入上界 1,048,576 Token，另预留相应输出上限；
对共享窗口而言是有意偏保守的全窗口预留，不是声称实际输入达到整个窗口。
主请求预留 1,056,576 Token / 3.217728 CNY；辅助预留 1,049,600 Token / 3.154944 CNY。
5 CNY 和 2,000,000 累计 Token 能准入首请求。
可靠 usage 结算后释放多余预留；下一请求必须同时满足新总额、单题额及累计 Token 的完整预留。
未知响应保留预留并停止；余额虽非零也可能因不足全窗口预留而提前结束。
预留不是实际消费，usage 折算费用也不是独立核实账单。

### 离线证据

复用 `p1-env`：Python 3.13.12、pytest 8.4.2、openai 2.36.0、httpx 0.28.1、Ruff 0.15.10。
pytest 满足项目 `>=7,<9`；未重建环境、未运行整套 P0 或全平台回归。
新增测试位于 `tests/evaluation/test_p1_continuation.py`，受控子进程仍复用 `p1_controlled_worker.py`。
全部使用临时合成 grant/前序账本及假 key；测试进程和子进程禁止外部 socket，
不加载真实授权文件、不把原 T01 现场作为可写测试目录。

组合测试实际经过组织器 → worker descriptor/freeze 校验 → 正式 headless CLI/Native
→ SDK/计费 transport → 受控响应。T01 materialize/worker 路径一旦出现即失败。
脚本化工具在临时 T04 fixture 修改文件，最终补丁在干净副本重放，目标 3/3、原回归 4/4；
这些是离线接线证据，不是模型成绩或 T04 真实验收。
正常分支的 8 个主请求保持 enabled/high/8000，1 个真实 sidecar 调用链请求保持
disabled/无 effort/1024/强制 emit_sidecar_verdict，得到 `accept/verifier_ok`，CLI 返回 0。
辅助缺 usage 时只有 9 个 inner 发送、未知后的 SDK 重试发送为 0，保留 3.154944 CNY 合成预留，
虽然运行 completed、补丁正确且产品降级 accept/provider_error，最终仍 infrastructure_blocked、CLI 返回 1。
worker 摘要缺失/不一致两条组合测试同样停止，保留补丁、诊断并将剩余预算标为未知。
账本 purpose 仍为 unknown，不用工具名/提示词授予权限。

私有离线证据目录：`.nz-coder-runs/p1-t04-preparation-20260908/`。
`red-plan.xml` 记录初始两个确定性失败（单题 grant 和 CLI）；
`red-receipt.xml` 记录失败落盘 receipt 仍被认可的反例，修复后对应测试通过；
`red-grant-format.xml` 记录预算格式问题，`green-grant-format.xml` 为 7 项通过。
`red-carryover.xml` 含新增模块尚未存在时的 setup errors，不作为业务失败复现证据；
早期 `contracts.xml` 的运行期间发生源码修改，出现 4 个组合测试失败，不作为最终结果。
最终命令在源码修改停止后重新执行：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .nz-coder-runs/p1-env/bin/python -m pytest -q \
  tests/evaluation/test_p1_live.py tests/evaluation/test_p1_billing.py \
  tests/evaluation/test_p1_sidecar.py tests/evaluation/test_p1_continuation.py \
  tests/test_headless_cli.py --tb=short \
  --junitxml=.nz-coder-runs/p1-t04-preparation-20260908/contracts-final.xml
.nz-coder-runs/p1-env/bin/python -m evaluation.linux_baseline.runner --help
.nz-coder-runs/p1-env/bin/python -m ruff check \
  evaluation/linux_baseline/continuation.py evaluation/linux_baseline/live.py \
  evaluation/linux_baseline/live_worker.py evaluation/linux_baseline/runner.py \
  tests/evaluation/test_p1_continuation.py tests/evaluation/test_p1_live.py \
  tests/evaluation/p1_controlled_worker.py --output-format concise
.nz-coder-runs/p1-env/bin/python -m py_compile \
  evaluation/linux_baseline/continuation.py evaluation/linux_baseline/live.py \
  evaluation/linux_baseline/live_worker.py evaluation/linux_baseline/runner.py \
  tests/evaluation/test_p1_continuation.py tests/evaluation/test_p1_live.py \
  tests/evaluation/p1_controlled_worker.py
git diff --check
```

只读代理复核了越权、参数冻结、重复分配及持久化边界；修改仅由主代理实施。
最终定向套件 **169 passed in 155.13s，退出码 0**；Ruff、编译、帮助及 `git diff --check` 均通过。
复核发现的 receipt 和金额格式问题均先复现后修复；文件/四级目录 fsync 失败均补测 worker 拒绝。
原 pair 的真实 freeze/descriptor 成功及预算重置拒绝另有回归。
项目未配置独立类型检查器，本轮未增加类型工具或依赖。

### 本轮真实任务与历史完整性

| 任务 | 本轮正式启动 | 本轮真实请求/重试 | 本轮独立验收 | 新 known / unknown 预留 | 状态 |
| --- | ---: | --- | --- | --- | --- |
| T04 | 0 | 0 / 0 | 未运行 | 0 / 0 CNY | 等待本次续接确认 |
| T01 | 0 | 0 / 0 | 未重跑，引用旧实验 | 0 / 0 CNY | 旧结果不变 |

其余 10 题本轮均未启动，仍 not_run；T04 真实成功率为 null，没有新真实补丁/Session/判定。
sidecar 本轮真实调用未覆盖，不发额外付费请求补齐覆盖。
旧 T01 正式启动仍为 1，目标 3/3、回归 4/4、patch_verified=true，
但旧最终分类仍 infrastructure_blocked；旧 known 0.2115294 CNY、unknown 3.154944 CNY 原样保留。
本轮新费用与新预留均 0，原账本余额仍 6.6335266 CNY；没有发生真实额度分配。
第14次的 HTTP 原因和实际账单仍未知。

上节列出的原授权、freeze、账本、补丁、结果、summary 及公开 result 的 SHA-256 均复核一致。
原文件未修改，现有 Session/诊断包/其他 worktree 未删除或重建。
只读进程核对为 0 个活动 baseline runner/worker；私有根目录仅有原实验 freeze，
未发现已有 T04 登记或后续真实实验支出证据。

本轮在真实调用前结束。下一步不是运行其余题，而是一次性确认：
只运行 T04 一次，最多使用原余额中的 5 CNY，保留旧未知预留；采用上述主/辅助模式和既有限制，
不重跑 T01、不运行其他任务，结束即停。
确认后才记录真实授权消息引用并创建新配置/实验，组织器 HOME 仍须在 workspace 外，
通过现有 `--live --live-config --output` 入口执行；本轮没有执行真实 live 命令。
