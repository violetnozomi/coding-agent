# 第 12 题：执行与学习记录

实例：`instance_qutebrowser__qutebrowser-cf06f4e3708f886032d4d2a30108c2fddb042d81-v2ef375ac784985212b1805e1d0431dc8f1b3c171`。Agent 状态 `max_turns`；官方有效结果 `True`，首次环境阻断 `False`。

实际主/辅助请求 40/0；已返回 usage 的峰时估算 2.45867024 元，非账单。

改变文件：scratch_proc_exp2.py, scratch_gp.py, scratch_proc_exp.py, tests/unit/misc/test_guiprocess.py, qutebrowser/misc/guiprocess.py

按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。

实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：

- executed=True, status=ok, command_failed=False: `ls /workspace && cat /workspace/setup.py 2>/dev/null | head -20; python --version`
- executed=False, status=error, command_failed=False: `ls /app 2>/dev/null && echo "---" && grep -rn "class GuiProcess" /app --include=*.py 2>/dev/null | head`
- executed=False, status=error, command_failed=False: `cd /workspace && grep -rln "GuiProcess" --include=*.py . | head`
- executed=False, status=error, command_failed=False: `cd /workspace && ls tests/unit/misc/ | head -50 && echo "---" && sed -n '1,200p' tests/unit/misc/test_guiprocess.py`
- executed=False, status=error, command_failed=False: `cd /workspace && python -c "import PyQt5; print(PyQt5.__file__)" && python -c "import PyQt5.QtCore as c; print(c.QT_VERSION_STR)"`
- executed=True, status=ok, command_failed=False: `python -c "import PyQt5.QtCore as c; print(c.QT_VERSION_STR)"`
- executed=False, status=error, command_failed=False: `cd /workspace && git log --oneline -5 && git status --short | head`
- executed=False, status=error, command_failed=False: `cd /workspace && ls doc/ | head; sed -n '1,40p' doc/changelog.asciidoc 2>/dev/null`
- executed=False, status=error, command_failed=False: `cd /tmp && cat > proc_exp.py <<'EOF' ↵ import sys ↵ from PyQt5.QtCore import QCoreApplication, QProcess, QTimer ↵  ↵ app = QCoreApplication(sys.argv) ↵  ↵ def run(script): ↵     proc = QProcess() ↵     counts = {'readyRead': 0, 'stdout': 0, 'stderr': 0} ↵     chunks = {'stdout': [], 'stderr': []} ↵     proc.setReadChannel(QProcess.StandardOutput) ↵     proc.readyRead.connect(lambda: counts.__setit`
- executed=True, status=ok, command_failed=False: `git -C /workspace log --oneline -3; echo "==="; git -C /workspace status --short`
- executed=False, status=error, command_failed=False: `cd /workspace && python scratch_proc_exp.py`
- executed=True, status=ok, command_failed=False: `python scratch_proc_exp.py`
- executed=True, status=nonzero, command_failed=True: `git -C /workspace log --oneline --all -- qutebrowser/misc/guiprocess.py | head -30`
- executed=True, status=ok, command_failed=False: `git -C /workspace branch -a; git -C /workspace tag | tail -5`
- executed=True, status=ok, command_failed=False: `python -m py_compile scratch_proc_exp.py`
- executed=True, status=error, command_failed=False: `python -m pytest tests/unit/misc/test_guiprocess.py -k "test_start_output_message" -x -q 2>&1 | tail -40`
- executed=True, status=nonzero, command_failed=True: `python -m pytest tests/unit/misc/test_guiprocess.py -k "test_start_output_message" -x -q 2>&1 | tail -40`
- executed=True, status=ok, command_failed=False: `python -m pytest tests/unit/misc/test_guiprocess.py -q 2>&1 | tail -40`
- executed=True, status=nonzero, command_failed=True: `pytest tests/unit/misc/test_guiprocess.py::test_start_output_message`
- executed=True, status=nonzero, command_failed=True: `pytest tests/unit/misc/test_guiprocess.py::test_start_output_message`
- executed=True, status=ok, command_failed=False: `python scratch_proc_exp2.py`
- executed=True, status=nonzero, command_failed=True: `pytest tests/unit/misc/test_guiprocess.py::test_start_output_message`
- executed=True, status=nonzero, command_failed=True: `pytest tests/unit/misc/test_guiprocess.py::test_start_output_message`
- executed=True, status=ok, command_failed=False: `git show HEAD:tests/unit/misc/test_guiprocess.py | sed -n '140,205p'`
- executed=True, status=ok, command_failed=False: `python scratch_gp.py 2>&1 | tail -5`

面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。
