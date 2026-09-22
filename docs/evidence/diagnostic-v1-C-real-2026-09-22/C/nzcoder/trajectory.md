# C real request trajectory

| Request | Trigger | Tools | G before/after | VG after | Unresolved | Finish |
|---|---|---|---|---|---|---|
| coding-1 (HTTP 1) | initial_investigation | read_file, bash | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-2 (HTTP 2) | investigation | bash | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-3 (HTTP 3) | investigation | list_directory | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-4 (HTTP 4) | investigation | read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-5 (HTTP 5) | investigation | read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-6 (HTTP 6) | investigation | read_file, read_file, read_file, read_file, read_file | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-7 (HTTP 7) | investigation | grep_search | 0 / 0 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-8 (HTTP 8) | investigation | write_file, write_file, write_file, write_file | 0 / 4 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-9 (HTTP 9) | verification | write_file, write_file | 4 / 6 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-10 (HTTP 10) | verification | bash, bash | 6 / 6 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-11 (HTTP 11) | verification | bash | 6 / 6 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-12 (HTTP 12) | verification | write_file | 6 / 7 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-13 (HTTP 13) | verification | bash | 7 / 7 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-14 (HTTP 14) | verification | bash | 7 / 7 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-15 (HTTP 15) | verification | write_files_batch | 7 / 7 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-16 (HTTP 16) | verification | write_files_batch | 7 / 8 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-17 (HTTP 17) | verification | bash, bash | 8 / 8 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-18 (HTTP 18) | convergence | read_file, read_file, read_file | 8 / 8 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-19 (HTTP 19) | convergence | read_file, read_file | 8 / 8 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-20 (HTTP 20) | convergence | edit_file, edit_file | 8 / 10 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-21 (HTTP 21) | convergence | bash, bash | 10 / 10 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-22 (HTTP 22) | convergence | write_file | 10 / 11 | -1 | ['R1', 'R2', 'R3'] | ['tool_calls'] |
| coding-23 (HTTP 23) | convergence | bash | 11 / 11 | 11 | ['R2'] | ['tool_calls'] |
| verifier-1 (HTTP 24) | verifier |  | 11 / 11 | 11 | ['R2'] | ['tool_calls'] |
| coding-24 (HTTP 25) | failure_repair |  | 11 / 11 | 11 | ['R2'] | ['stop'] |
| verifier-2 (HTTP 26) | verifier |  | 11 / 11 | 11 | [] | ['tool_calls'] |
