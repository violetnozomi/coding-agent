# 第 04 题：执行与学习记录

实例：`instance_ansible__ansible-395e5e20fab9cad517243372fa3c3c5d9e09ab2a-v7eee2454f617569fd6889f2211f75bc02a35f9f8`。Agent 状态 `max_turns`；官方有效结果 `True`，首次环境阻断 `False`。

实际主/辅助请求 40/0；已返回 usage 的峰时估算 2.27587928 元，非账单。

改变文件：_scratch_enum_probe.py, lib/ansible/executor/play_iterator.py, lib/ansible/plugins/strategy/linear.py, lib/ansible/plugins/strategy/__init__.py

按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。

实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：

- executed=False, status=error, command_failed=False: `cd /workspace && git log --oneline -3 && git status`
- executed=False, status=error, command_failed=False: `ls /workspace && ls /app 2>/dev/null | head`
- executed=False, status=error, command_failed=False: `cd /workspace && python --version && cat lib/ansible/release.py | head -20`
- executed=False, status=error, command_failed=False: `cd /workspace && python -c "import sys; print(sys.version)"`
- executed=True, status=ok, command_failed=False: `echo hi`
- executed=True, status=ok, command_failed=False: `python3 --version; python2 --version 2>/dev/null; which pytest; python3 -c "import ansible; print(ansible.__version__)" 2>&1 | tail -1`
- executed=False, status=error, command_failed=False: `cd /tmp && python3 - <<'EOF' ↵ from enum import IntEnum, IntFlag ↵  ↵ class IteratingStates(IntEnum): ↵     SETUP = 0 ↵     TASKS = 1 ↵     RESCUE = 2 ↵     ALWAYS = 3 ↵     COMPLETE = 4 ↵  ↵ class FailedStates(IntFlag): ↵     NONE = 0 ↵     SETUP = 1 ↵     TASKS = 2 ↵     RESCUE = 4 ↵     ALWAYS = 8 ↵  ↵ print(repr(IteratingStates(0).name), str(IteratingStates(0))) ↵ for n in (0,1,2,4,8,3,15,16):`
- executed=False, status=error, command_failed=False: `cd /workspace && python3 - <<'EOF' ↵ from enum import IntEnum, IntFlag ↵  ↵ class IteratingStates(IntEnum): ↵     SETUP = 0 ↵     TASKS = 1 ↵     RESCUE = 2 ↵     ALWAYS = 3 ↵     COMPLETE = 4 ↵  ↵ class FailedStates(IntFlag): ↵     NONE = 0 ↵     SETUP = 1 ↵     TASKS = 2 ↵     RESCUE = 4 ↵     ALWAYS = 8 ↵  ↵ print(repr(IteratingStates(0).name), str(IteratingStates(0))) ↵ for n in (0,1,2,4,8,3,1`
- executed=False, status=error, command_failed=False: `cd /workspace && python3 _scratch_enum_probe.py`
- executed=True, status=ok, command_failed=False: `python3 _scratch_enum_probe.py`
- executed=False, status=error, command_failed=False: `cd /workspace && grep -n "PlayIterator\|play_iterator" lib/ansible/plugins/strategy/__init__.py lib/ansible/plugins/strategy/linear.py`
- executed=True, status=ok, command_failed=False: `python -m py_compile _scratch_enum_probe.py`
- executed=True, status=ok, command_failed=False: `python -m py_compile _scratch_enum_probe.py`
- executed=False, status=error, command_failed=False: `cd /workspace && git diff -- lib/ansible/plugins/strategy/linear.py | head -60`
- executed=True, status=ok, command_failed=False: `python -m py_compile _scratch_enum_probe.py`
- executed=True, status=ok, command_failed=False: `python -m py_compile _scratch_enum_probe.py`
- executed=True, status=ok, command_failed=False: `python -m py_compile _scratch_enum_probe.py`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m py_compile lib/ansible/executor/play_iterator.py lib/ansible/plugins/strategy/__init__.py lib/ansible/plugins/strategy/linear.py && echo COMPILE_OK && rm -f _scratch_enum_probe.py && python -m pytest test/units/executor/test_play_iterator.py -q 2>&1 | tail -15`
- executed=False, status=error, command_failed=False: `cd /workspace && python -m py_compile lib/ansible/executor/play_iterator.py lib/ansible/plugins/strategy/__init__.py lib/ansible/plugins/strategy/linear.py && echo COMPILE_OK`

面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。


## 本题复盘要点

任务要求把 PlayIterator 的整数状态迁移为公开的具名类型，同时保留旧名称的类级/实例级访问和弃用提示，并让 HostState 展示可读状态。实际补丁修改迭代器与两处 strategy 调用方，另留下一个 scratch 探测脚本；文件列表和精确变更见 model.patch，不能把探测脚本当成产品需求。

官方在全新原始项目镜像应用本次冻结补丁与固定测试补丁，必需测试 8/8 通过。Agent 自己的测试/探测调用、拒绝与真实执行见 command-evidence.json；官方通过不追认那些未执行调用。Agent 用满 40 个主请求并以 max_turns 结束，没有将外部验收结果反写成 completed。

面试时可解释三个具体问题：枚举与位标志为何采用不同类型；旧整数常量的兼容入口如何发出弃用警告而不改变值；跨模块迁移为何仍需验证旧插件访问。再对照轨迹指出哪些探测推动了修复、哪些调用没有执行，以及为何功能验收通过与 Agent 正常结束是两项独立结论。
