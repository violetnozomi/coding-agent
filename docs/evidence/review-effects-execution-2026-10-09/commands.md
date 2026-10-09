# 命令与结果

以下 `NZ_EXEC_DIR` 为私有运行产物目录；公开副本将原宿主路径脱敏。prepare/probe/verify/status/help/online 拒绝路径不读取真实模型密钥；上游仅为绑定 loopback 的本地假服务。依赖下载是环境准备的单独外网步骤。

## 复用当前产物

```bash
python scripts/review_effects_execution.py --help
python scripts/review_effects_execution.py verify --probe-id final --positive-probe-id supervisor --artifact-root "$NZ_EXEC_DIR"
python scripts/review_effects_execution.py status --artifact-root "$NZ_EXEC_DIR"
python scripts/review_effects_execution.py online --artifact-root "$NZ_EXEC_DIR"
```

help、verify、status 退出 0，online 退出 2。状态见 readiness.json、online-refusal.json；模型请求为 0。verify 核验冻结 manifest、两版运行源码、实际 Docker 配置/挂载、驱动、真实结果和账目；status 对回执、账目及产物哈希再核对。

## 全新离线验收目录

不要将 NZ_EXEC_DIR 指向已有或失败的目录来清理重试。新目录准备 wheels 后执行：

```bash
mkdir -p "$NZ_EXEC_DIR/wheels" "$NZ_EXEC_DIR/download-home"
env -i PATH="$PATH" HOME="$NZ_EXEC_DIR/download-home" LANG=C.UTF-8 PIP_CONFIG_FILE=/dev/null \
  python -m pip download --disable-pip-version-check --index-url https://pypi.org/simple \
  --require-hashes --no-deps --only-binary=:all: --python-version 312 --implementation cp --abi cp312 \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --platform linux_x86_64 \
  --dest "$NZ_EXEC_DIR/wheels" -r docs/evidence/review-effects-execution-2026-10-09/requirements.lock
python scripts/review_effects_execution.py prepare-runtime --artifact-root "$NZ_EXEC_DIR"
python scripts/review_effects_execution.py probe --probe-id initial --artifact-root "$NZ_EXEC_DIR"
python scripts/review_effects_execution.py verify --probe-id initial --artifact-root "$NZ_EXEC_DIR"
```

基础镜像需事先存在：`python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`。环境允许时可另行从官方 Docker 来源准备该摘要镜像；启动器不通过 npx 或自动网络下载运行时。实际本轮使用了本地已有镜像，并构建 `--network=none --pull=false`。无需 sudo、privileged 或全局配置修改。

本轮最初 PyPI 下载是根版本列表解析后冻结全部 wheel 哈希，详见 dependency-download.txt；上述命令直接复用最终完整锁。Docker 构建结果 image 为 `sha256:ad95168761bfd58549057442c026c24cb863a5bf2f3511c8f0bae5c462c49641`。源码 bundle 不包含整个仓库。

## 实际执行记录

本轮 prepare-runtime 退出 0；旧冻结材料 verify 输出 PASS；最终接受正向 `--probe-id supervisor --probe-modes positive`、取消/期限 `--probe-id final`。合并核验不是重写原始轨迹，三个回执都绑定具体目录和哈希。原来的 initial、role-fix、cancel-fix、write-check 及 final 正向记录保留，派生关系见 provenance.json。

```bash
env -u OPENAI_API_KEY -u DEEPSEEK_API_KEY -u NZ_RUN_PAIRED_SMOKE PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q -rs tests/evaluation/test_model_relay.py tests/evaluation/test_paired_runtime_smoke.py
python -m ruff check nz_coder/evaluation/model_relay.py tests/evaluation/test_model_relay.py \
  tests/evaluation/fixtures/review_execution_entry.py tests/evaluation/fixtures/review_boundary_probe.py \
  tests/evaluation/fixtures/review_execution_fake.py scripts/review_effects_execution.py
```

前者 32 passed、1 skipped，27.13 秒；后者 All checks passed。SDK/Gateway 重试使用本地服务；既有 offline_exec 的后代非 Unix 网络拒绝回归也运行了。未开启 InfCodeX opt-in、未执行全仓测试。旧 Core 的真实 Docker 导入命令与结果见 old-runtime-import-command.json、old-runtime-import-cwd-fixed.txt；初始 cwd 配置错误见 old-runtime-import.txt。

假服务物理请求不作为正式实验用量：接受的正向 5、取消 4、期限 12，各有独立离线 ledger；包含调试尝试的全部容器假请求为 56。预算单测另有独立本地假服务，按测试账本核验，不将它们混入冻结的 26 次线上提案。真实远程模型请求、费用、效果运行均未发生。
