# 本次命令与实际结果

符号目录仅脱敏路径：`<RUNTIME>` 为上轮冻结 Docker 环境，`<COUNTER>` 为既有官方 recipe/tokenizer，`<RAW_RUN>` 为本轮受控原件目录。没有重新安装依赖或重建镜像。

首次新增经验准入回归在原实现得到 **16 failed、1 passed**：新接口不存在，strict 对照已拒绝参考计数。实现后第一次局部回归 **64 passed、1 deselected**，随后包括实际隔离 Runner 的回归 **74 passed**。最后增加启动器源码绑定和事件时间记录，运行以下最终相关检查：

```sh
PYTHONPATH=<COUNTER>/recipe-site \
NZ_DEEPSEEK_V41_TOKENIZER=<COUNTER>/v41-tokenizer.json \
NZ_REVIEW_RUNTIME_ROOT=<RUNTIME> \
python -m pytest -q \
  tests/evaluation/test_empirical_review_execution.py \
  tests/evaluation/test_model_relay.py \
  tests/evaluation/test_deepseek_counting.py \
  tests/evaluation/test_review_formal_entry.py \
  tests/evaluation/test_reference_adapter.py
```

**105 passed，81.71 秒，无 skipped。**本地上游全部为回环假服务，真实旧／新生产 Runner/read/edit/Bash/Judge 运行；假模型 usage 不并入线上结果。

```sh
python -m ruff check nz_coder/evaluation/model_relay.py \
  scripts/review_effects_execution.py \
  tests/evaluation/test_empirical_review_execution.py \
  tests/evaluation/fixtures/review_execution_fake.py
git diff --check
```

通过。必要适配提交 `cc7a43c53499cc4212cc100d06606119615aab81`，生产 Core 没有修改。

实际宿主调用以下入口一次。密钥由宿主从项目 `.env` 读入专用环境变量，仅传给宿主；容器只有 Unix socket 和本地假值，不继承真实密钥、代理或在线 fallback。调用 argv 原件保留；这里不列密钥赋值。

```sh
PYTHONPATH=<COUNTER>/recipe-site \
python scripts/review_effects_execution.py online \
  --artifact-root <RUNTIME> \
  --tokenizer <COUNTER>/v41-tokenizer.json \
  --authorization <RAW_RUN>/authorization.json \
  --input-mode empirical \
  --output-root <RAW_RUN>/formal
```

退出 **0**；u01→u02→u03→u04→a01；12 个实际 Provider 请求，u03 为 0；无传输重试或自动重跑。启动时 `online_not_run=true` 是未发送前的状态，最终 [results.json](results.json) 为 false，不能用启动状态否认已发生请求。

冻结终态后复用原 `agent_core_diagnostic_v1/validate.py` 的 `snapshot`、`project_tests`、`acceptance`、`execute`；独立进程使用已有 `offline_exec.py` 拒绝网络，不加载模型。A 的检查源码 SHA 与原 manifest 相符。G/D 使用原冻结的三组验收代码；原候选、在线文件和冻结副本哈希均核对。

- 四个 G/D 项目复测均通过；G 独立 3/3，D 2/3，仅默认负数拒绝失败。
- A 实际 Agent 子进程及冻结副本 `python -m pytest -q tests` 均为 **14 passed**，外部九组 **9/9**；None、空串、跨层、JSON/文本、CLI 与 README 分别验收。
- 只读取冻结终态，没有套用 oracle，没有让模型看外部验收，也没有继续请求模型。

公开证据执行原件索引／哈希、JSON/JSONL、链接、推理正文与密钥泄漏检查；授权使用记录保留，活动授权已关闭。全仓、Windows 和历史 compaction 测试本轮未运行，未宣称通过。
