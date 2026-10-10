# SWE-bench 官方协议适配：2026-10-10

基线与开工 HEAD 均为 `969e4e6f49a7589814e47986c10324566ea0a5d6`，没有后续提交或用户工作树修改。保留 Python Core、原生 Runner、验证、Artifact、版本保护和权限边界。本次修改仅在既有 SWE 适配、复现记录和预算转发准入中。执行时是基线加未提交修改；代码交付提交为 `565223d`，不冒充旧基线的运行结果。

**本轮完成离线生产链回归和一次真实官方 Docker 评分自检，没有运行收费模型。** 官方自检使用 evaluator-only 金补丁，不能计为 Agent 解题成绩。2 个校准、10 个正式实例均未启动自主解题，不报告 resolved 比例或全量 Verified 成绩。

## 规则与实施映射

采用 `swebench==4.1.0` 的实际 `--help`；官方公开文档核查日为 2026-10-10，并不声称安装包等于当前 upstream HEAD。

| 类型 | 核查来源/要求 | 本轮处理 |
|---|---|---|
| 官方预测/评分协议 | [Evaluation guide](https://www.swebench.com/SWE-bench/guides/evaluation/)、[Harness](https://www.swebench.com/SWE-bench/reference/harness/)、[CLI](https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/cli.md)：标准 JSONL 与独立官方 harness | 两条写入路径只输出 instance_id/model_name_or_path/model_patch；状态另存。实际传 split/ids/namespace/tag，安装版没有 arch 参数，非本机架构明确拒绝 |
| 官方缓存 | [FAQ](https://www.swebench.com/SWE-bench/faq/)：run_id/instance 缓存不能证明新补丁已经评分 | 绑定预测、dataset-file 哈希、split、实例、镜像配置、harness 版本；同 run_id 换 patch/config 拒绝 |
| 官方投稿材料 | [Experiments README](https://github.com/SWE-bench/experiments/blob/main/README.md)、[Checklist](https://github.com/SWE-bench/experiments/blob/main/checklist.md)：预测、轨迹、日志、技术报告及披露 | 既有本地校验器不等于维护者认证；manifest 明确 local_candidate_check_only、maintainer_verified=false。未知官方裁决不能进入已完整评分的投稿包 |
| 本轮实验策略 | Single Attempt；一次运行内允许多轮修改/复测；不做 Best@k；无答案检索网络 | 不把“三文件内编辑”“一次验证”“禁止新文件”称为官方规则；retry-agent 仍是独立诊断轨道，不替换首轮 |
| 本项目实现缺陷 | F1–F5，旧路径、裁剪展示回调、输入混杂、缓存及分母 | 以下均有实际模块回归；不移植新 Agent 循环或评分器 |

## 五项缺陷与回归

| 项目 | 原失败与最小修改 | 实际验收/状态 |
|---|---|---|
| F1 真实补丁丢失 | 失败状态在两个预测写入路径被清空；采集还删除新增 test_* 并改变真实 index。改为临时 index 的二进制 diff，原文件/index 保留；异常后尽力采集，Agent 状态/adapter 错误/patch/评分分开 | 真实 staged+unstaged+new+deleted+rename；独立 base 应用；completed/max_turns/timeout/error 同补丁。实际 Provider 故障前已完成编辑：基线 run_batch 写空，当前标准 JSONL 保留修改。**已修复** |
| F2 隐形重做 | open claim 不能证明没请求，却删除重建并继续 attempt=1。现保留并返回 interrupted；已完成跳过。复用文件锁，绑定工作根目录和预测输出；换输出不能绕过 owner | 真实请求和写入之后、journal 结果 fsync 之前中断；resume 无新请求/Session、不删第一份修改及轨迹。第二个真实 CLI 换输出被拒绝。**已修复，未新增通用续接引擎** |
| F3 工具/提示不一致 | strict 缺 read_tool_result/repo_context，提示有无根据的强制探索与验证限制。复用已有本地查询、当前 Session 不透明读取，review_run_evidence 仅按既有 advisory 职责开放，轨迹校验同一白名单 | 真实请求含 schema；准入后确实执行 repo_context、read_tool_result、文件编辑和失败→通过 pytest。移除旧限制，安全边界保留。**已修复本轮范围**；其他项目原生 runner 仍有未验证准入 |
| F4 旧归档路径 | 改用当前 Session/Artifact API，只归档绑定 Session；真实执行事实、版本、补丁、证据索引随包保存，按配额流式复制 | 包移动且原存储删除后，ArtifactStore 实际读取唯一失败证据；其他 Session 不进入包；满配额不删除源。**已修复**。TraceRecorder 仍是投影，HTTP 缺失明确标 unavailable，不称其无损 |
| F5 存活后代 | 外层只终止 multiprocessing worker。现先停止调度/协作取消/close，再有界等待和既有进程树兜底；Linux 跟踪本 worker 后代及启动身份，包括另建进程组的 Bash | 真实 Bash pytest 后代持续写文件：基线超时后继续写，修复后停止，且无迟到模型请求。确认停止后才能返回冻结路径；不能确认则 unstable，不伪造确定 patch。**Linux 已验证；Windows 未运行** |

[回归代码](../../../tests/evaluation/test_swebench_protocol.py)、[公开输入与预算准入回归](../../../tests/evaluation/test_swebench_inference_input.py)、[测试输出](test-results.txt)、[命令](commands.json)。旧测试的无效 `{}` 预测、未提交 Git fixture、已废弃 sessions 位置及期望“重做 open claim”均改为有效输入和替代行为断言；不删除断言或全局跳过。

测试中另遇到了长临时路径超出 Unix socket 限制、旧 fixture 共享评分 run_id、宿主测试网络替身污染，以及第二个 Docker 复用容器名。这些是本轮新出现并核实的测试环境问题，不登记为历史 Core 失败。当前使用短且独立的临时路径、spawn 隔离模型替身及独立容器名；容器最后只清理本测试拥有的名字。

## 两条真实离线执行证据

**原生 SWE 工具链（明确标记的人造公开回归项目，不是 SWE-bench 实例成绩）：** repo_context → read_file → 真实 pytest 失败 → 模型收到失败及 Artifact ID → 实际 read_tool_result → edit_file → 同一测试真实通过 → runtime-owned 复测 → 原生 max_turns。

[实际受控 Provider 请求](native-replay/actual-provider-requests.jsonl)、[执行事实/版本](native-replay/execution-facts.jsonl)、[投影 trace](native-replay/trace.jsonl)、[可恢复 Artifact 索引](native-replay/tool-results/index.json)、[补丁](native-replay/patch.diff)、[冻结独立复测](native-replay/independent-test.txt)、[结果](native-replay/result.json)。真实工具执行和测试进程均未被 mock；只有 Provider 使用预定协议响应，不替 Agent 写文件或运行测试。

实际边界记录 7 个受控主请求、0 个受控辅助请求，usage 是测试值。失败 command_failed=true、dispatch_failed=false；修改 generation=1 后验证通过。原生完成门仍返回 completion_gate_requires_more_work、最终 max_turns；不事后改为 completed，不把独立测试通过反推所有需求完成。本轮不展开完成门专项。该链只能证明证据与控制流，不证明模型自主修复能力。

**真实 CLI + spawn + 同一预算账本：** 干净容器、fresh HOME、环境白名单、network=none、非 root、cap-drop、只挂载自己的公开初态和代码；没有评分数据、宿主管理 socket、真实密钥、历史记忆。调用现有 SWE CLI，所有主/辅助模型通过既有 Unix 转发；服务仅监听回环地址，SDK 自动重试=0。read_file 与 compact 实际执行，主请求 2、摘要辅助请求 1；总预留 3 后下一请求拒绝，后续本地拒绝不是新增上游模型请求。第二次 CLI 改输出仍被工作根 owner 拒绝。

[账本](relay-cli/ledger.jsonl)、[实际本地请求/响应](relay-cli/provider/)、[原生结果](relay-cli/predictions.report.json)、[配置清单](relay-cli/predictions.manifest.json)、[owned archive](relay-cli/owned-archive/evidence-index.json)、[stdout](relay-cli/stdout.txt)。本地 fixture 使用 JSON 字节作为测试计数，**不是真实模型 tokenizer 或线上效率值**。原始文件在宿主私有目录保留，公开副本经逐文件脱敏；[索引](evidence-index.json)保留原件与公开副本哈希。公开副本的哈希不冒充原件哈希。

## 一次真正的官方 Docker 评分

`harness-self-test-gold` / `sympy__sympy-22914` / `nz-adapter-self-test-20261010`，固定官方数据集 revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`。金补丁和 test_patch/测试选择只在 evaluator-only 数据行与官方 harness 内；推理侧未接收。没有 mock subprocess、harness 或 resolved 算法。

在 evaluator-only 工作目录实际运行：

```bash
HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 python -m nz_coder.swebench run-eval \
  --profile verified --dataset-file evaluator-only.json --split test \
  --predictions-path predictions.jsonl --instance-ids sympy__sympy-22914 \
  --run-id nz-adapter-self-test-20261010 --timeout 300 --max-workers 1 \
  --image-namespace swebench --image-arch x86_64 --instance-image-tag latest --no-package
```

真实 harness 退出 0；[原官方单例 report](official-self-test/report.json)明确 patch_successfully_applied=true、resolved=true；[原官方汇总](official-self-test/harness-self-test-gold.nz-adapter-self-test-20261010.json)为 submitted/completed/resolved 各 1，errors=0；[输出](official-self-test/test_output.txt)、[评分脚本](official-self-test/eval.sh)、[补丁](official-self-test/patch.diff)、[命令回执](official-self-test/command-output.txt)。[本地投影](official-self-test/evaluation-results.json)单独存储，不改官方原件；它在自检后离线读取已有报告生成，不是第二次评分。

这是 **harness self-test**。不能说 NZ-Coder solved 1 SWE instance，不能与正式 Agent 推理混计。新增反例已验证：进程退出 0，但 missing/字符串 `resolved` 仍为 null/unknown；预拉失败与空 patch 保留在分母，不伪造单例报告。

## 环境、样本与停止边界

[执行身份/镜像](execution.json)、[环境准备](environment/)、[选择方法与排除表](selection.json)、[校准公开输入](inputs/calibration.json)、[正式公开输入](inputs/formal.json)。仅用四字段 instance_id/repo/base_commit/problem_statement；真实运行请求断言隐藏标注 sentinel 未出现。源码与 clone 的 original_base_commit / original_tree / local_head / local_tree 分别记录，清除未来 Git 历史后的 tree 必须相同，不称新 HEAD 是原 commit。

宿主 Linux/Python 3.13.12，swebench 4.1.0、datasets 4.0.0、Docker SDK 7.1.0、daemon 29.1.3。通用 `nz-swe-runtime:20261010` 仅用于回归。SymPy 官方镜像 digest `sha256:aa5f1cf12ce166511d03eed6ac0de0f76b27777ac2c6ddb4a9b11f67f000941f`；只提取 testbed 依赖环境到独立推理镜像，未复制 /testbed、.git 或评分脚本。测试 Python 是 3.9.20，没有 pytest；公开初态 bin/test test_sympify 实际结果 45 passed、5 skipped、2 expected failures（这不是隐藏验收）。Agent 自身仍使用基镜像 Python 3.12，项目测试解释器必须明确选择；当前 strict 对 bin/test 的准入未证明，不说两个环境等价。

Astropy 官方 manifest 可读取，匹配镜像一个 layer 六分钟仍未完成，停止所拥有的下载客户端，环境为 blocked。未改全局代理、未删其他 Docker 对象、未自动再拉其他大镜像。裸 CLI 本身不提供操作系统沙箱，未经外部匹配环境和隔离回执的 raw-checkout 应标为“受限源码环境”；strict 声明和默认 false 标记不证明隔离已经完成。正式运行仍需复用现有隔离启动器，不能直接在有密钥/数据集缓存的宿主上运行项目测试。依赖下载、镜像构建和官方评分进程与模型费用分别记录；镜像/依赖没有作为 Agent 生成补丁。

冻结选择使用 seed=20261010，排除项目已知 37 个历史暴露 ID 后，从 463 个候选按 SHA256(seed+换行+ID) 排序；前 2 校准、随后 10 正式。更广泛训练/个人历史暴露未知，不填零。

| 用途 | 实例 | Agent / patch / official | 尚缺 |
|---|---|---|---|
| 校准 | sympy__sympy-22914 | not_run / not_captured / null | 当前费用授权、项目 runner 准入/有效环境口径；金补丁自检不计此列 |
| 校准 | astropy__astropy-7166 | not_run / not_captured / null | 当前费用授权、匹配环境 |
| 正式 | django__django-11820 | not_run / not_captured / null | 当前费用授权、实例环境与执行回执 |
| 正式 | matplotlib__matplotlib-25960 | not_run / not_captured / null | 同上 |
| 正式 | scikit-learn__scikit-learn-11310 | not_run / not_captured / null | 同上 |
| 正式 | django__django-13363 | not_run / not_captured / null | 同上 |
| 正式 | django__django-12039 | not_run / not_captured / null | 同上 |
| 正式 | django__django-13513 | not_run / not_captured / null | 同上 |
| 正式 | sympy__sympy-12096 | not_run / not_captured / null | 同上 |
| 正式 | django__django-16560 | not_run / not_captured / null | 同上 |
| 正式 | django__django-15278 | not_run / not_captured / null | 同上 |
| 正式 | pydata__xarray-4695 | not_run / not_captured / null | 同上 |

已付费模型请求 0；主/辅助/重试/embedding 未真实出站。没有本阶段有效收费授权，历史 G/D/A/B/N 额度不沿用，也没有把助手此前自行选择的“10 元”变成用户授权。若后续进入付费阶段，需一次明确提供：币种/总额、主辅合计请求/token/期限边界、实例范围与模型实际参数，并先完成隔离和预算准入回执；本轮不创建授权文件或反复询问。

## 测试结果与未覆盖项

- 适配层、strict、归档、真实工具/CLI：158 passed（当时版本，完整启用本地 Docker）。后续 owner 收尾 84 passed，唯一失败为第二个 CLI 的测试容器名冲突；仅修测试名称后对应真实 CLI 回归 1 passed，断言维持。
- 新增的真实 Provider 失败→预测保存：固定基线 spawn 回放确实输出空预测、回归失败；当前真实 spawn 路径通过（包含在 owner 的 84 passed）。不是仅证明 patch_status 字段存在。
- 相关预算转发/公开输入/实验执行等一次联合运行：225 passed、2 skipped；跳过分别是未显式启用本地 Docker，以及未指定已有 frozen review runtime/tokenizer。Docker 项本轮已单独实际启用并通过，不重复计入总数。
- Core、Node 验证、Skill、编辑恢复、Artifact 与事务保留回归：367 passed、5 skipped；五项依赖 Windows 原生句柄/junction，Linux 未执行。
- 超时/取消、实际请求中断的最终清理回归：3 passed；ruff、compileall、源码及结构化材料的 git diff --check 通过。官方原始 diff/log 保留必需上下文空白；不为消除行尾空白提示改写它们。未运行 Windows、全仓测试或真实模型解题。

两项历史 compaction fake：test_context_overflow_stops_after_three_compaction_attempts、test_pre_send_and_reactive_compactions_share_one_three_attempt_owner；基线复现证据仍见 [N 审计](../n-terminal-audit-2026-09-19/README.md)和其 test-results。本轮未重跑、未修改或弱化，不把本轮出现的新 fixture 失败归入它们，也不宣称全仓通过。

方法修复、离线生产链、实际官方评分自检是三个证据层级；真实自主 SWE 效果还没有证据。本轮到协议可信和可复核为止，不扩建 Core，也不生成面试用的虚构成功案例。
