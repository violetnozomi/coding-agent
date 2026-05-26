# NZ-Coder 2026-05-17 改进总结

基于 SWE-bench Lite 实测数据分析和 Claude Code 源码研究，本轮实施了以下改进：

---

## 一、新增 4 个 Repo Intelligence 工具

**文件**: `nz_coder/tools/repo_intel.py`（新增）

| 工具 | 功能 | 解决的问题 |
|------|------|-----------|
| `diff_status` | 显示当前 git diff 状态（文件、大小、是否改了测试）+ 下一步建议 | Agent 不知道自己是否有 diff |
| `verify_changed_files` | 对 changed .py 文件跑 `py_compile`，不跑 pytest | Agent 陷入本地测试噪音循环 |
| `read_symbol` | AST 级符号读取：`mode=read` 读函数/类/方法源码，`mode=list` 列出文件所有符号 | 省去 grep→read_file 猜 offset 的多轮往返 |
| `smart_search` | 从 issue/traceback 提取 token，grep-first 策略找候选文件，返回排序+AST 摘要 | 替代多轮 grep_search 探索 |

### 2026-05-17 追加：read_symbol 增强 + find_symbol_callers

| 功能 | 模式 | 说明 |
|------|------|------|
| `read_symbol(mode="list")` | LSP documentSymbol 等价 | 列出文件所有 top-level 符号（函数/类/方法）及行号 |
| `find_symbol_callers` | LSP findReferences 等价 | AST 遍历全 repo 找到目标符号的所有引用（call/attr/name/decorator） |

---

## 二、Claude Code 风格的 State-as-Message 机制

**文件**: `nz_coder/runtime_state.py`（新增）

### 核心设计

```
AgentLoop 每轮调用 LLM 前:
  system_content = system_prompt + memory_block + state_block + scratch_block
                                                    ^^^^^^^^^^^
                                                    自动注入的客观状态
```

### RuntimeState 跟踪的状态

| 类别 | 字段 | 更新方式 |
|------|------|----------|
| Turn/Time | `turn_count`, `max_turns`, `started_at`, `timeout_seconds` | run() 开始时 reset，每轮更新 |
| Edit | `last_edit_turn`, `edits_this_run` | write/edit/apply_patch 工具调用时更新 |
| Diff | `has_diff`, `diff_chars`, `changed_files`, `tests_modified` | diff_status 工具调用时解析 |
| Verification | `verification_attempts`, `py_compile_ok`, `broad_test_attempts`, `env_noise_seen` | verify_changed_files/bash 工具调用时更新 |
| Search | `searched_patterns`, `read_files` | grep_search/read_file 调用时记录 |
| Transition | `transition` | 每次工具调用后更新（edited_source/searched/verified/...） |

### 状态块触发规则

| 条件 | 注入的提醒 |
|------|-----------|
| 始终 | `Turn N/M \| Time Xs/Ys \| Z turns remaining` |
| `has_diff=True` | `SOURCE DIFF EXISTS: N chars across M files. Run verify_changed_files and finalize.` |
| 连续 5+ 轮无编辑 | `No source edit in N turns. Stop broad exploration.` |
| 连续 8+ 轮无编辑 | `WARNING: No source edit in N turns. Make the minimal source change NOW.` |
| ≥3 次 broad test | `STOP: broad test runs attempted. Use verify_changed_files only.` |
| 检测到 env noise | `Environment noise detected. Test failures may NOT be caused by your patch.` |
| ≤5 turns 剩余且有 diff | `CRITICAL: Only N turns remaining. Finalize your patch NOW.` |
| ≤10 turns 剩余且有 diff | `Low budget: N turns remaining. Verify and finalize.` |
| <60s 剩余且有 diff | `CRITICAL: Less than 60s remaining. Finalize your patch NOW.` |
| py_compile OK + diff 存在 | `py_compile passed. Source diff exists. You should FINALIZE now.` |

### Broad Test 拦截

当 `diff_status` 检测到 source diff 后，`config.BLOCK_BROAD_TESTS=True`，bash 工具会阻止 `pytest tests/` 等 broad test runner。

---

## 三、grep_search 重大升级（对标 Claude Code GrepTool）

**文件**: `nz_coder/tools/search.py`（重写）

### 变更对比

| 维度 | 旧版 | 新版 |
|------|------|------|
| 默认模式 | `content`（返回匹配行） | **`files_with_matches`**（返回文件路径） |
| 排序 | 无 | **按 mtime 排序**（最近修改优先） |
| 输出模式 | 仅 content | `files_with_matches` / `content` / `count` 三种 |
| 分页 | 无 | `head_limit` + `offset` 分页 |
| 大小写 | 默认敏感 | `case_insensitive` 开关 |
| 上下文 | 无 | `context` 参数（content 模式） |

### 新增参数

```
output_mode: "files_with_matches" | "content" | "count"
head_limit: 默认 50 (files/count) 或 250 (content)
offset: 跳过的结果数
context: 上下文行数（content 模式）
case_insensitive: 大小写不敏感
```

---

## 四、系统提示词更新

**文件**: `nz_coder/prompt.py`

### 新增工具列表项
```
- find_symbol_callers
- smart_search (grep-first)
- read_symbol (mode: read/list)
- grep_search (files_with_matches default, mtime sorted)
```

### 新增行为指令
```
- For code search, use grep_search or smart_search. NEVER use bash grep/rg.
- grep_search defaults to files_with_matches (sorted by mtime). Use read_symbol on the top file before reading it entirely.
```

---

## 五、SWE-bench 验证协议重写

**文件**: `nz_coder/swebench/orchestrator.py`

旧的验证指导：
> "run the most relevant tests you can"

新的验证协议：
```
1. Start with smart_search using issue statement, failing tests, traceback.
2. Inspect at most 3 candidate files before making the first edit.
3. Prefer read_symbol over read_file when a symbol name is known.
4. After any source edit, call diff_status.
5. If diff_status shows source-only diff, call verify_changed_files.
6. Do NOT run pytest, tox, or full test suites.
7. If verify_changed_files passes, finalize.
8. If local tests fail due to env issues, stop verifying.
9. A plausible non-empty source patch is better than no patch.
```

---

## 六、修改文件清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `nz_coder/tools/repo_intel.py` | **新增** | 5 个工具：diff_status, verify_changed_files, read_symbol (mode=read/list), smart_search (grep-first), find_symbol_callers |
| `nz_coder/runtime_state.py` | **新增** | RuntimeState dataclass + observe_tool + build_prompt_block |
| `nz_coder/tools/search.py` | **重写** | grep_search: files_with_matches 默认, mtime 排序, content/count 模式, head_limit/offset 分页 |
| `nz_coder/loop.py` | 修改 | 导入 RuntimeState；每轮注入 state_block；工具后调用 observe_tool；broad test 拦截 |
| `nz_coder/prompt.py` | 修改 | 新增 5 个工具到工具列表；新增搜索行为指令 |
| `nz_coder/tools/bash.py` | 修改 | 导入 _is_broad_test_command；当 BLOCK_BROAD_TESTS=True 时阻止 broad test |
| `nz_coder/swebench/orchestrator.py` | 修改 | SWE-bench prompt 替换为 search-and-verification protocol |

---

## 七、验证

- ✅ `python3 -m py_compile` 所有修改文件通过
- ✅ 91 tests passed (`python3 -m pytest -q`)
- ✅ 17 工具注册成功，无重名
- ✅ grep_search 三种模式 smoke test 通过
- ✅ read_symbol list/read 模式 smoke test 通过
- ✅ find_symbol_callers smoke test 通过
- ✅ smart_search grep-first 策略 smoke test 通过
- ✅ RuntimeState 端到端 6 轮 workflow 模拟通过
- ✅ Broad test blocking smoke test 通过

---

## 八、设计参考

以下设计直接借鉴自 Claude Code 源码：

| Claude Code 特性 | NZ-Coder 实现 |
|------------------|---------------|
| GrepTool 默认 `files_with_matches` + mtime 排序 | `grep_search` 新版默认 |
| LSPTool `documentSymbol` | `read_symbol(mode="list")` |
| LSPTool `findReferences` | `find_symbol_callers` |
| State-as-message `<system-reminder>` 注入 | `RuntimeState.build_prompt_block()` |
| Token budget continuation nudge | Turn/time 预算提醒 |
| Diminishing returns 检测 | 空转检测（5/8轮无编辑） |
| Broad test 拦截 | `config.BLOCK_BROAD_TESTS` + bash 检查 |
| 硬约束 "NEVER bash grep" | prompt 行为指令 |
| 搜索委托 "Use Agent for open-ended searches" | smart_search 作为统一入口 |
