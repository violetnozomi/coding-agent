# 第 20 题：执行与学习记录

实例：`instance_internetarchive__openlibrary-d8162c226a9d576f094dc1830c4c1ffd0be2dd17-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c`。Agent 状态 `completed`；官方有效结果 `True`，首次环境阻断 `False`。

实际主/辅助请求 15/0；已返回 usage 的峰时估算 0.26343648 元，非账单。

改变文件：openlibrary/tests/catalog/test_utils.py, openlibrary/catalog/utils/__init__.py

按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。

实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：

- executed=True, status=ok, command_failed=False: `ls /workspace && echo "---" && ls /workspace/openlibrary/catalog/utils/`
- executed=True, status=ok, command_failed=False: `ls /workspace/tests/ | head -50; echo "---"; find /workspace/tests -name "*util*" -o -name "*import*" | head -40`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m pytest openlibrary/tests/catalog/test_utils.py -q 2>&1 | tail -20`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/tests/catalog/test_utils.py -q 2>&1 | tail -20`
- executed=True, status=error, command_failed=False: `python -m pytest openlibrary/tests/catalog -q 2>&1 | tail -5`
- executed=True, status=ok, command_failed=False: `git diff`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/tests/catalog/test_utils.py -q 2>&1 | tail -3; python -c "from openlibrary.catalog.utils import get_non_isbn_asin, is_asin_only; print(get_non_isbn_asin({'source_records':['amazon:B000KRRIZI']}), is_asin_only({'source_records':['amazon:B000KRRIZI']}))"`

面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。
