# M / nzcoder：实际逐请求轨迹

编号为实际 HTTP 请求，辅助单列。V 是请求前文件版本；G/AG/VG 分别为 mutation、acceptance mutation 和 verification generation。工具结果不足以判断退出事实时保留 unknown。每行完整参数、原工具输出、模型可见历史和需求状态见 causal-table.json。

| 请求 / purpose / 主预算余额 | trigger / finish_reason | 模型动作 | 工具事实与状态 | 请求前版本 / ledger | Provider prompt / completion |
|---|---|---|---|---|---|
| 1 / coding #1 / 24 | initial_investigation / tool_calls | bash find . -type f -not -path "./.git/*" -not -path "*/__pycache__/*" \\| head -50 && echo "---" && cat REQUIREMENTS.md; list_directory . | bash: 未执行 / dispatch failed; list_directory: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 7758 / 129 |
| 2 / coding #2 / 23 | investigation / tool_calls | read_file REQUIREMENTS.md; read_file README.md; read_file billing/legacy.py; read_file billing/__init__.py | read_file: 执行; read_file: 执行; read_file: 执行; read_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 7952 / 144 |
| 3 / coding #3 / 22 | investigation / tool_calls | read_file web.py; read_file cli.py; read_file jobs.py; read_file mobile.py; read_file reporting.py; read_file tests/test_consumers.py | read_file: 执行; read_file: 执行; read_file: 执行; read_file: 执行; read_file: 执行; read_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 8814 / 189 |
| 4 / coding #4 / 21 | investigation / tool_calls | write_file billing/quote.py; write_file web.py; write_file cli.py; write_file jobs.py; write_file mobile.py; write_file reporting.py | write_file: 执行; write_file: 执行; write_file: 执行; write_file: 执行; write_file: 执行; write_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 9644 / 2558 |
| 5 / coding #5 / 20 | verification / tool_calls | write_file tests/test_consumers.py; write_file tests/test_quote.py | write_file: 执行; write_file: 执行 | V1; G/AG/VG=6/6/-1; R1=pending,R2=pending,R3=pending,R4=pending | 15423 / 2525 |
| 6 / coding #6 / 19 | verification / tool_calls | bash python -m pytest -q tests | bash: 执行 / command failed / exit=1 | V2; G/AG/VG=8/8/-1; R1=pending,R2=pending,R3=pending,R4=pending | 18115 / 585 |
| 7 / coding #7 / 18 | failure_repair / tool_calls | edit_file tests/test_quote.py | edit_file: 执行 | V2; G/AG/VG=8/8/-1; R1=pending,R2=pending,R3=pending,R4=pending | 15687 / 348 |
| 8 / coding #8 / 17 | failure_repair / tool_calls | bash git diff --stat && echo "=== original test_consumers ===" && git show HEAD:tests/test_consumers.py 2>/dev/null; echo "=== original… | bash: 未执行 / dispatch failed | V3; G/AG/VG=9/9/-1; R1=pending,R2=pending,R3=pending,R4=pending | 17596 / 397 |
| 9 / coding #9 / 16 | failure_repair / tool_calls | diff_status | diff_status: 执行 | V3; G/AG/VG=9/9/-1; R1=pending,R2=pending,R3=pending,R4=pending | 16467 / 143 |
| 10 / coding #10 / 15 | failure_repair / tool_calls | bash git show HEAD:tests/test_consumers.py | bash: 未执行 / dispatch failed | V3; G/AG/VG=9/9/-1; R1=pending,R2=pending,R3=pending,R4=pending | 16625 / 183 |
| 11 / coding #11 / 14 | failure_repair / tool_calls | read_file tests/test_consumers.py | read_file: 执行 | V3; G/AG/VG=9/9/-1; R1=pending,R2=pending,R3=pending,R4=pending | 16687 / 317 |
| 12 / coding #12 / 13 | failure_repair / tool_calls | apply_patch cli.py; apply_patch mobile.py; apply_patch billing/quote.py | apply_patch: 执行; apply_patch: 执行; apply_patch: 执行 | V3; G/AG/VG=9/9/-1; R1=pending,R2=pending,R3=pending,R4=pending | 17204 / 1303 |
| 13 / coding #13 / 12 | failure_repair / tool_calls | bash git diff -- tests/test_consumers.py | bash: 未执行 / dispatch failed | V4; G/AG/VG=12/12/-1; R1=pending,R2=pending,R3=pending,R4=pending | 19961 / 371 |
| 14 / coding #14 / 11 | failure_repair / tool_calls | apply_patch tests/test_consumers.py; write_file README.md | apply_patch: 执行; write_file: 执行 | V4; G/AG/VG=12/12/-1; R1=pending,R2=pending,R3=pending,R4=pending | 18720 / 1637 |
| 15 / coding #15 / 10 | failure_repair / tool_calls | bash python -m pytest -q tests | bash: 执行 / exit=0 | V5; G/AG/VG=14/13/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 21729 / 91 |
| 16 / verifier #1 / 9 | verifier / tool_calls | emit_sidecar_verdict | 辅助模型结构化 verdict；消费结果见 runtime-full.jsonl | V5; G/AG/VG=14/13/14; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 9859 / 346 |
| 17 / coding #16 / 9 | failure_repair / tool_calls | glob_search | glob_search: 执行 | V5; G/AG/VG=14/13/14; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 20199 / 1768 |
| 18 / coding #17 / 8 | failure_repair / tool_calls | bash git log --oneline -5; echo "---"; git status --short; bash ls -a; ls .git 2>/dev/null \\| head | bash: 未执行 / dispatch failed; bash: 未执行 / dispatch failed | V5; G/AG/VG=14/13/14; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 20543 / 1703 |
| 19 / coding #18 / 7 | failure_repair / tool_calls | write_files_batch web.py, jobs.py, cli.py, mobile.py, reporting.py, tests/test_consumers.py | write_files_batch: 执行; bash [runtime]: 未执行 / dispatch failed | V5; G/AG/VG=14/13/14; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 20686 / 7336 |
| 20 / coding #19 / 6 | failure_repair / tool_calls | bash python -m pytest -q tests 2>&1 \\| tail -30 | bash: 执行 / command failed / exit=1 | V6; G/AG/VG=15/15/14; R1=candidate,R2=satisfied,R3=candidate,R4=candidate | 23781 / 57 |
| 21 / coding #20 / 5 | failure_repair / tool_calls | apply_patch tests/test_consumers.py | apply_patch: 执行 | V6; G/AG/VG=15/15/14; R1=candidate,R2=satisfied,R3=candidate,R4=candidate | 23223 / 358 |
| 22 / coding #21 / 4 | failure_repair / tool_calls | apply_patch README.md | apply_patch: 执行 | V7; G/AG/VG=16/16/14; R1=candidate,R2=satisfied,R3=candidate,R4=candidate | 24585 / 695 |
| 23 / coding #22 / 3 | failure_repair / tool_calls | bash python -m pytest -q tests 2>&1 \\| tail -20 | bash: 执行 / exit=0 | V8; G/AG/VG=17/16/14; R1=candidate,R2=candidate,R3=candidate,R4=candidate | 25367 / 51 |
| 24 / coding #23 / 2 | failure_repair / tool_calls | verify_changed_files | verify_changed_files: 执行 | V8; G/AG/VG=17/16/14; R1=candidate,R2=candidate,R3=candidate,R4=candidate | 24807 / 200 |
| 25 / coding #24 / 1 | failure_repair / stop | 自然语言 final | bash [runtime]: 执行 / exit=0 | V8; G/AG/VG=17/16/17; R1=candidate,R2=candidate,R3=candidate,R4=candidate | 25012 / 1159 |
| 26 / verifier #2 / 0 | verifier / tool_calls | emit_sidecar_verdict | 辅助模型结构化 verdict；消费结果见 runtime-full.jsonl | V8; G/AG/VG=17/16/17; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 10768 / 412 |

主模型后续实际能看到哪些工具结果，以下一请求的 model_visible_tool_results 为准。终答后的 runtime-owned 验证可能不再进入任何主请求；不能倒推模型看见了最终 ledger。

最终终端原事实：
```json
"max_turns"
```

冻结独立验收整体通过：False。usage 见 usage.json；费用 cost unknown。
