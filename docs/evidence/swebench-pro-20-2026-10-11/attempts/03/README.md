# 第 03 题：执行与学习记录

实例：`instance_internetarchive__openlibrary-9cd47f4dc21e273320d9e30d889c864f8cb20ccf-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4`。Agent 状态 `completed`；官方有效结果 `True`，首次环境阻断 `True`。

实际主/辅助请求 36/0；已返回 usage 的峰时估算 1.00920312 元，非账单。

改变文件：openlibrary/catalog/add_book/__init__.py, openlibrary/catalog/add_book/tests/test_add_book.py

按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。

实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：

- executed=True, status=ok, command_failed=False: `ls /workspace && echo "---" && ls /workspace/openlibrary/catalog/add_book/ 2>/dev/null`
- executed=True, status=ok, command_failed=False: `ls -R /workspace/openlibrary/catalog/add_book/tests | head -50; echo ---; ls /workspace/tests | head -50`
- executed=True, status=ok, command_failed=False: `grep -n "cover" /workspace/openlibrary/catalog/add_book/tests/test_add_book.py | head -40; echo ---; wc -l /workspace/openlibrary/catalog/add_book/tests/test_add_book.py`
- executed=False, status=error, command_failed=False: `grep -rn "media-amazon" /workspace/conf /workspace/config /workspace/openlibrary 2>/dev/null | grep -v node_modules | head -30`
- executed=False, status=error, command_failed=False: `grep -rni "allow.*host\|host.*allow\|coverstore.*host\|AllowedHosts" /workspace/conf /workspace/openlibrary/plugins/upstream 2>/dev/null | head -30`
- executed=False, status=error, command_failed=False: `sed -n 1,40p /workspace/openlibrary/catalog/add_book/tests/conftest.py; echo ---; grep -rn "def build_query" /workspace/openlibrary/catalog/add_book/load_book.py`
- executed=True, status=ok, command_failed=False: `grep -n "cover" /workspace/openlibrary/catalog/add_book/__init__.py`
- executed=False, status=error, command_failed=False: `cd /workspace && git status --short && echo --- && git log --oneline -3`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -x -q 2>&1 | tail -25`
- executed=True, status=error, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -q 2>&1 | tail -25`
- executed=True, status=nonzero, command_failed=True: `python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -q 2>&1 | tail -25`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -q 2>&1 | tail -5`
- executed=True, status=error, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/ -q 2>&1 | tail -5`
- executed=True, status=ok, command_failed=False: `python -m py_compile openlibrary/catalog/add_book/__init__.py`
- executed=True, status=error, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -q 2>&1 | tail -5`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/test_add_book.py -q 2>&1 | tail -5`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/catalog/add_book/tests/test_load_book.py -q 2>&1 | tail -4`
- executed=True, status=ok, command_failed=False: `sed -n '900,1000p' openlibrary/catalog/add_book/__init__.py`
- executed=True, status=ok, command_failed=False: `sed -n '1000,1070p' openlibrary/catalog/add_book/__init__.py`
- executed=True, status=ok, command_failed=False: `sed -n '555,595p' openlibrary/catalog/add_book/__init__.py`
- executed=True, status=ok, command_failed=False: `pytest openlibrary/catalog/add_book/tests/test_add_book.py::test_covers_are_added_to_edition`
- executed=True, status=ok, command_failed=False: `pytest openlibrary/catalog/add_book/tests/test_add_book.py -q -k "cover or ProcessCover" 2>&1 | tail -4`

面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。


本题学习重点：用 urllib.parse.urlparse 解析协议和 hostname，使用 casefold 比较精确允许主机，统一处理导入与已有 edition 的封面下载入口；避免字符串前缀白名单误放子域。真实补丁与调用方接入见 model.patch。

官方必需集合是 6 个 process_cover_url 参数用例，均通过，因此固定 verifier reward=1。全文件 stdout 为 83 passed、1 failed、3 rerun；原有 test_covers_are_added_to_edition 在官方恢复测试正文后仍失败（matched 与 modified 不一致）。不能将本题概括成“所有测试通过”。首次插件参数阻断的日志仍保留，纠正验收只用同一冻结补丁、未调用或重跑模型。
