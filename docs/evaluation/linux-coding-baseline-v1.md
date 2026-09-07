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
