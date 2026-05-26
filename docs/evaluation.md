# NZ-Coder Evaluation

NZ-Coder includes a benchmark harness for testing coding-agent behavior without relying on anecdotal demos.

## Running

```bash
python -m nz_coder.benchmark --list
python -m nz_coder.benchmark
python -m nz_coder.benchmark --report
```

Outputs:

- `.nz-coder/benchmark/report.json`
- `.nz-coder/benchmark/report.md`
- `.nz-coder/benchmark/runs/*.jsonl`

## Benchmark Results

| 运行时间 | 分数 | 模型 | 备注 |
|---------|------|------|------|
| 2025-05 首次运行 | 9/13 (69%) | deepseek-v4-pro | `completed_unverified` 被误判为 FAIL |
| 2025-05 修复后 | **13/13 (100%)** | deepseek-v4-pro | 修复 `_HARD_FAIL_STATUSES` 白名单 bug 后 |

**Bug 根因**：`benchmark.py` 的 `run_task()` 原本用 `status != "completed"` 判断失败，把合法的 `completed_unverified` 状态也当成失败。修复后改为白名单：`{"aborted", "max_turns", "error"}`。

## Task Coverage

The benchmark currently covers:

- file creation
- bug fixing
- feature addition
- test authoring
- test repair
- multi-file debugging
- CLI behavior changes
- structured JSON edits
- refactoring while preserving public API
- documentation updates

## Metrics

Each task records:

- pass/fail and verification reason
- task type and difficulty
- duration
- assistant turns
- tool calls and tool errors
- trace path

The report aggregates pass rate, average turns/tools/time, pass rate by difficulty, pass rate by task type, and failure categories.

## SWE-bench Lite 官方评测

SWE-bench Lite 使用 Docker 隔离环境运行，通过 `python3 -m nz_coder.swebench_lite` 子命令操作。

### 环境要求

- **Python ≥ 3.9**（swebench 使用了 `list[X]` 等 3.9+ 类型语法）
- **Docker Engine**（评测时每个 issue 会启动独立容器）
- **系统 Python 3.8 的解决方案**：在 Docker 容器（Python 3.11）内运行完整流程

### Docker 运行方式

```bash
# 构建评测镜像（Python 3.11 + swebench + datasets）
docker build --network host \
  --build-arg HTTP_PROXY=http://127.0.0.1:7890 \
  --build-arg HTTPS_PROXY=http://127.0.0.1:7890 \
  -f docker/swebench-runner.Dockerfile \
  -t nz-coder-swebench-runner .

# 验证环境（挂载 docker socket；无需挂宿主 docker CLI）
docker run --rm \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $PWD:/work \
  -w /work \
  nz-coder-swebench-runner \
  python -m nz_coder.swebench_lite check

# 生成 predictions（需要 API key / .env）
docker run --rm \
  --network host \
  --env-file .env \
  -e HTTP_PROXY=http://127.0.0.1:7890 \
  -e HTTPS_PROXY=http://127.0.0.1:7890 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $PWD:/work \
  -v /tmp:/tmp \
  -w /work \
  nz-coder-swebench-runner \
  python -m nz_coder.swebench_lite run-agent \
    --max-instances 5 \
    --output /work/.nz-coder/swebench-lite/predictions.jsonl

# 运行官方评测
docker run --rm \
  --network host \
  -e HTTP_PROXY=http://127.0.0.1:7890 \
  -e HTTPS_PROXY=http://127.0.0.1:7890 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $PWD:/work \
  -v /tmp:/tmp \
  -w /work \
  nz-coder-swebench-runner \
  python -m nz_coder.swebench_lite run-eval \
    --predictions-path /work/.nz-coder/swebench-lite/predictions.jsonl \
    --run-id my-run
```

> **注意**：官方 harness 使用 Python Docker SDK，可以只挂载 `/var/run/docker.sock`。如果镜像构建阶段要走本机代理，必须加 `docker build --network host`，否则 Docker build 容器里的 `127.0.0.1:7890` 指向的是构建容器自身。

### 子命令

| 子命令 | 功能 |
|--------|------|
| `check` | 检查运行环境（swebench/datasets/git/docker） |
| `run-agent` | 运行 agent 为每个 issue 生成 patch，输出 `predictions.jsonl` |
| `run-eval` | 调用官方 `swebench.harness.run_evaluation`，返回 resolved/unresolved |
| `retry-agent` | 基于官方失败日志重试失败的 instance |

## Unit-Level Runtime Tests

`tests/test_loop_fake.py` uses a fake OpenAI-compatible client to test the agent loop without calling a real model. It verifies:

- tool-call execution followed by final response
- invalid tool JSON feedback
- transient API retry
- trace event creation
