# 实际命令与结果

所有模型调用均为本地假 HTTP 服务，远程模型调用 0。资源下载单独记录于 resource-hashes.json/official-doc-sources.json；没有模型探测或 max_tokens=1 请求。以下路径是此次实际私有目录，可在复用环境中替换。

```sh
PYTHONPATH=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/recipe-site \
NZ_DEEPSEEK_V41_TOKENIZER=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/v41-tokenizer.json \
NZ_REVIEW_RUNTIME_ROOT=/home/pyh/.codex/artifacts/nz-review-execution-2026-10-09 \
python -m pytest -q tests/evaluation/test_model_relay.py tests/evaluation/test_deepseek_counting.py tests/evaluation/test_review_formal_entry.py tests/evaluation/test_reference_adapter.py
```

85 passed，66.59 秒。第一次增加启动器条件检查后，原 status 无资源回归暴露 parser-error；现已恢复“输出阻断状态”的行为，原断言未弱化，最终全部通过。

```sh
python -m pytest -q -rs tests/evaluation/test_paired_runtime_smoke.py::test_offline_launcher_blocks_network_after_exec_and_child_spawn tests/evaluation/test_paired_runtime_smoke.py::test_real_paired_entrypoints
```

1 passed、1 skipped。skipped 是未启用 opt-in 的真实 InfCodeX 成对入口，本轮明确不启动。最初误写不存在的测试名导致 collection exit=4，修正名称后得到上述结果；该 collection 错误不是通过或历史 Core 失败。

```sh
PYTHONPATH=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/recipe-site \
python tests/evaluation/fixtures/offline_exec.py python -m pytest -q \
/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/audited-recipe/deepseek-recipe-python/tests/test_bindings.py::test_rendering \
/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/audited-recipe/deepseek-recipe-python/tests/test_bindings.py::test_encoding_with_tokenizer
```

官方测试文件原样下载，仅恢复其相对 tokenizer 布局；2 passed，0.19 秒。未运行官方多模态/远程图片测试。

```sh
python scripts/review_effects_execution.py formal-local \
  --artifact-root /home/pyh/.codex/artifacts/nz-review-execution-2026-10-09 \
  --output-root /home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/formal-local-second
```

实际运行 5 个容器、14 个假上游请求、远程 0；另在 pytest 临时目录重复验证受影响的新代码，最终原始 regular files 封存于私有 formal-verified（socket 不归档）。所有实验目录独立且拒绝覆盖。初次正式入口尝试退出 2、上游 0：runtime 的初次 prepared driver hash 与最终 probe driver 不同，改为核验真实 probe 回执及基线 Git blob，未篡改历史记录。

```sh
PYTHONPATH=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/recipe-site \
python docs/evidence/provider-contract-2026-10-09/compare_history.py \
  /home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/v41-tokenizer.json \
  docs/evidence/provider-contract-2026-10-09/historical-counts.json
```

40 个历史请求，23 个本地参考值可用，23 个 delta=0，17 个形状不支持。只读历史记录；新请求 0。

```sh
PYTHONPATH=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/recipe-site \
python scripts/review_effects_execution.py status \
  --artifact-root /home/pyh/.codex/artifacts/nz-review-execution-2026-10-09 \
  --tokenizer /home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/v41-tokenizer.json
```

退出 0，明确状态：isolation/output true，strict input/budget/technical/authorization false。

```sh
PYTHONPATH=/home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/recipe-site \
python tests/evaluation/fixtures/offline_exec.py python scripts/review_effects_execution.py online \
  --artifact-root /home/pyh/.codex/artifacts/nz-review-execution-2026-10-09 \
  --tokenizer /home/pyh/.codex/artifacts/nz-provider-contract-2026-10-09/official/v41-tokenizer.json
```

退出 2，密钥访问/执行前拒绝。未来入口可附加 `--authorization <宿主授权文件>`，但授权文件不能补齐缺失的严格计数契约。其字段需匹配源码中的 authorization_valid，ready=true 不参与判断；本轮没有创建有效费用授权。环境变量 `NZ_REVIEW_DEEPSEEK_API_KEY` 只在全部条件成立后读取，本轮未读。

```sh
python -m ruff check nz_coder/evaluation/model_relay.py nz_coder/evaluation/deepseek_counting.py scripts/review_effects_execution.py tests/evaluation/test_model_relay.py tests/evaluation/test_deepseek_counting.py tests/evaluation/test_review_formal_entry.py tests/evaluation/fixtures/review_execution_entry.py tests/evaluation/fixtures/review_execution_fake.py docs/evidence/provider-contract-2026-10-09/compare_history.py
git diff --check
```

通过。未运行生产 Core 全仓、Windows 或 compaction 专项；本轮修改范围没有这些模块。

```sh
python -m pytest -q tests/evaluation/test_model_relay.py::test_contract_failure_keeps_already_sent_other_role_truthful
```

新增回归单独 1 passed，1.17 秒。没有修改生产代码；沿用已通过的85项结果。已经发出的另一个角色请求仍计入物理尝试、取消后未知usage保留预留，后续请求不再到上游。
