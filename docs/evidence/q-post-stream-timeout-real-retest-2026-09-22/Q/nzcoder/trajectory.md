# Q / nzcoder：实际逐请求轨迹

编号为实际 HTTP 请求，辅助单列。V 是请求前文件版本；G/AG/VG 分别为 mutation、acceptance mutation 和 verification generation。工具结果不足以判断退出事实时保留 unknown。每行完整参数、原工具输出、模型可见历史和需求状态见 causal-table.json。

| 请求 / purpose / 主预算余额 | trigger / finish_reason | 模型动作 | 工具事实与状态 | 请求前版本 / ledger | Provider prompt / completion |
|---|---|---|---|---|---|
| 1 / coding #1 / 24 | initial_investigation / tool_calls | bash find . -type f -not -path './node_modules/*' -not -path './.git/*' \\| head -50; echo '---'; ls -la; read_file REQUIREMENTS.md | bash: 未执行 / dispatch failed; read_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 7869 / 112 |
| 2 / coding #2 / 23 | investigation / tool_calls | read_file lib/pool.cjs; read_file lib/batch.cjs; read_file index.cjs; read_file README.md; list_directory . | read_file: 执行; read_file: 执行; read_file: 执行; read_file: 执行; list_directory: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 8440 / 199 |
| 3 / coding #3 / 22 | investigation / tool_calls | read_file tests/pool.test.cjs; read_file tests/batch.test.cjs; read_file package.json | read_file: 执行; read_file: 执行; read_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 9099 / 105 |
| 4 / coding #4 / 21 | investigation / tool_calls | write_file lib/pool.cjs; write_file lib/batch.cjs | write_file: 执行; write_file: 执行 | V0; G/AG/VG=0/0/-1; R1=pending,R2=pending,R3=pending,R4=pending | 9647 / 10204 |
| 5 / coding #5 / 20 | verification / tool_calls | write_file tests/pool.test.cjs; write_file tests/batch.test.cjs | write_file: 执行; write_file: 执行 | V1; G/AG/VG=2/2/-1; R1=pending,R2=pending,R3=pending,R4=pending | 12764 / 3120 |
| 6 / coding #6 / 19 | verification / tool_calls | write_file README.md | write_file: 执行 | V2; G/AG/VG=4/4/-1; R1=pending,R2=pending,R3=pending,R4=pending | 18145 / 724 |
| 7 / coding #7 / 18 | verification / tool_calls | bash node --test tests/pool.test.cjs tests/batch.test.cjs 2>&1 \\| tail -40 | bash: 执行 / exit=0 | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 16683 / 88 |
| 8 / coding #8 / 17 | verification / tool_calls | bash git status --porcelain; echo '---'; git stash list; echo '--- baseline show of pool head ---'; git diff --stat; verify_changed_files | bash: 未执行 / dispatch failed; verify_changed_files: 执行 | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 16509 / 757 |
| 9 / coding #9 / 16 | verification / tool_calls | bash git diff --stat | bash: 未执行 / dispatch failed | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 16666 / 87 |
| 10 / coding #10 / 15 | verification / tool_calls | read_file lib/pool.cjs | read_file: 执行 | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 16722 / 257 |
| 11 / coding #11 / 14 | verification / tool_calls | bash git status --porcelain | bash: 未执行 / dispatch failed | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 17452 / 620 |
| 12 / coding #12 / 13 | verification / stop | 自然语言 final | bash [runtime]: 执行 / exit=0 | V3; G/AG/VG=5/4/-1; R1=pending,R2=candidate,R3=pending,R4=pending | 17509 / 716 |
| 13 / verifier #1 / 12 | verifier / tool_calls | emit_sidecar_verdict | 辅助模型结构化 verdict；消费结果见 runtime-full.jsonl | V3; G/AG/VG=5/4/5; R1=satisfied,R2=satisfied,R3=candidate,R4=satisfied | 11243 / 389 |

主模型后续实际能看到哪些工具结果，以下一请求的 model_visible_tool_results 为准。终答后的 runtime-owned 验证可能不再进入任何主请求；不能倒推模型看见了最终 ledger。

最终终端原事实：
```json
"completed"
```

冻结独立验收整体通过：True。usage 见 usage.json；费用 cost unknown。
