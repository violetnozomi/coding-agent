# 第 02 题：执行与学习记录

实例：`instance_internetarchive__openlibrary-dbbd9d539c6d4fd45d5be9662aa19b6d664b5137-v08d8e8889ec945ab821fb156c04c7d2e2810debb`。Agent 状态 `completed`；官方有效结果 `True`，首次环境阻断 `True`。

实际主/辅助请求 40/0；已返回 usage 的峰时估算 1.92289536 元，非账单。

改变文件：openlibrary/plugins/upstream/utils.py, openlibrary/plugins/openlibrary/lists.py, openlibrary/plugins/openlibrary/tests/test_lists.py

按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。

实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：

- executed=False, status=error, command_failed=False: `cd /workspace && ls -la && git log --oneline -5 2>/dev/null`
- executed=False, status=error, command_failed=False: `cd /workspace 2>/dev/null || cd /app; pwd; ls`
- executed=False, status=error, command_failed=False: `pwd && ls -la`
- executed=True, status=ok, command_failed=False: `ls openlibrary/plugins/upstream/`
- executed=True, status=ok, command_failed=False: `python -c "import web; print(web.__file__); print(web.__version__ if hasattr(web,'__version__') else 'n/a')"`
- executed=True, status=ok, command_failed=False: `python - <<'EOF' ↵ import inspect, web ↵ print(inspect.getsource(web.data)) ↵ print("====INPUT====") ↵ print(inspect.getsource(web.input)) ↵ EOF`
- executed=False, status=error, command_failed=False: `cd /workspace && git log --oneline -8 2>&1 | head; echo ---; git status --short 2>&1 | head -30`
- executed=True, status=ok, command_failed=False: `echo hello`
- executed=True, status=ok, command_failed=False: `python - <<'EOF' ↵ import inspect, web.utils, web.webapi ↵ print(inspect.getsource(web.utils.storify)) ↵ print("===rawinput===") ↵ print(inspect.getsource(web.webapi.rawinput)) ↵ EOF`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m pytest --doctest-modules openlibrary/plugins/upstream/utils.py -q 2>&1 | tail -20`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m pytest openlibrary/plugins/openlibrary/tests/test_lists.py openlibrary/plugins/upstream/tests/test_utils.py -q 2>&1 | tail -20`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/plugins/upstream/tests/test_utils.py -q 2>&1 | tail -20`
- executed=True, status=nonzero, command_failed=True: `python -m pytest --doctest-modules openlibrary/plugins/upstream/utils.py -q 2>&1 | tail -25`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/plugins/openlibrary/tests/test_lists.py -q 2>&1 | tail -25`
- executed=True, status=ok, command_failed=False: `python -m pytest openlibrary/plugins/upstream/utils.py::openlibrary.plugins.upstream.utils.unflatten -q 2>&1 | tail -5; echo ===; git -C /workspace stash list 2>/dev/null; git -C /workspace diff --stat 2>/dev/null`
- executed=False, status=error, command_failed=False: `cd /workspace && git stash && python -m pytest --doctest-modules openlibrary/plugins/upstream/utils.py -q 2>&1 | tail -6; git stash pop`
- executed=True, status=ok, command_failed=False: `python -m py_compile openlibrary/plugins/upstream/utils.py`
- executed=True, status=ok, command_failed=False: `python -m py_compile openlibrary/plugins/upstream/utils.py`
- executed=False, status=error, command_failed=False: `cd /workspace && git diff openlibrary/plugins/upstream/utils.py`
- executed=False, status=error, command_failed=False: `cd /workspace && sed -n '1,80p' pyproject.toml 2>/dev/null; echo ===; sed -n '1,60p' setup.cfg 2>/dev/null; echo ===; sed -n '1,60p' pytest.ini 2>/dev/null`
- executed=False, status=error, command_failed=False: `cd /workspace && python -c " ↵ from openlibrary.plugins.upstream.utils import unflatten ↵ print(unflatten({'a':1,'b--x':2,'b--y':3,'c--0':4,'c--1':5})) ↵ print(unflatten({'seeds--0--key':'/books/OL1M','name':'x'})) ↵ print(unflatten({'a':'first','a--0':'ignored'})) ↵ print(dict(unflatten({'k':'v','k':'w'}))) ↵ "`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m pytest openlibrary/plugins/openlibrary/tests/ -q 2>&1 | tail -15`
- executed=True, status=nonzero, command_failed=True: `pytest openlibrary/plugins/upstream/utils.py::openlibrary`
- executed=True, status=nonzero, command_failed=True: `pytest openlibrary/plugins/upstream/utils.py::openlibrary`
- executed=True, status=ok, command_failed=False: `pytest openlibrary/plugins/openlibrary/tests/test_lists.py openlibrary/plugins/upstream/tests/test_utils.py -q -p no:cacheprovider`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m doctest openlibrary/plugins/upstream/utils.py -v 2>&1 | tail -5; python -c " ↵ from openlibrary.plugins.upstream.utils import unflatten ↵ assert unflatten({'a':1,'b--x':2,'b--y':3,'c--0':4,'c--1':5}) == {'a':1,'c':[4,5],'b':{'y':3,'x':2}} ↵ assert unflatten({'a--0--x':1,'a--0--y':2,'a--1--x':3,'a--1--y':4}) == {'a':[{'x':1,'y':2},{'x':3,'y':4}]} ↵ assert unflatten({'seed`
- executed=False, status=error, command_failed=False: `cd /workspace && python -c " ↵ from openlibrary.plugins.upstream.utils import unflatten ↵ assert unflatten({'a':1,'b--x':2,'b--y':3,'c--0':4,'c--1':5}) == {'a':1,'c':[4,5],'b':{'y':3,'x':2}} ↵ assert unflatten({'a--0--x':1,'a--0--y':2,'a--1--x':3,'a--1--y':4}) == {'a':[{'x':1,'y':2},{'x':3,'y':4}]} ↵ assert unflatten({'seeds--0--key':'/books/OL1M','name':'x'})['seeds'] == [{'key':'/books/OL1M'}] ↵`

面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。

第 02 题有效官方复评：同一补丁 SHA-256 `ef02acf857a49a6a555ef0315bce0800e4eadffade9152c261076de5bbb8a129`、同一原始镜像 digest，恢复官方插件加载后 6/6 必需测试通过，reward=1、退出 0。没有重跑 Agent，没有新模型调用。原 reward=0 对应测试未启动的环境阻断，原文件保留，不能当作功能失败再声称修复代码。

本题 Agent 期间还受到启动依赖路径的影响；官方复评从始至终未添加 Agent 依赖。模型修改涉及 POST 原始 body 的解码/解析、默认值与嵌套祖先键的关系、种子规范化、简单键覆写。面试重点是 query/body 的边界、urlencoded 的加号/重复键、unflatten 的父子键冲突、类型和空项处理；应从请求/补丁核对具体实现，不能只记模型自述。
