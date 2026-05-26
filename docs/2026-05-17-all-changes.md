# NZ-Coder 本轮全部代码修改记录

> 日期：2026-05-17
> 基线：git initial commit `69125af`
> 触发：SWE-bench Lite 测试分析 → Claude Code 源码研究 → 系统性改进

---

## 修改概览

| 类别 | 文件数 | 说明 |
|------|--------|------|
| 新增文件 | 2 | repo_intel.py, runtime_state.py |
| 重写文件 | 1 | search.py (grep_search) |
| 修改文件 | 4 | loop.py, prompt.py, bash.py, orchestrator.py |
| 文档新增 | 3 | swebench-failure-analysis.md, 2026-05-17-improvements.md, 2026-05-17-all-changes.md |

---

## 一、新增文件

### 1. `nz_coder/tools/repo_intel.py`

5 个新工具，对标 Claude Code 的搜索和代码智能工具。

```python
# 工具注册（均通过 nz_coder.tools.register）
- diff_status           # 显示 git diff 状态 + 下一步建议
- verify_changed_files  # py_compile 验证（不跑 pytest）
- read_symbol           # AST 符号读取：mode=read（读源码）/ mode=list（列出全部符号）
- smart_search          # token 提取 + grep-first 候选文件 + AST 摘要
- find_symbol_callers   # AST 遍历全 repo 找符号的所有引用
```

**设计参考**：
- `grep_search` 默认 `files_with_matches` — 源码：[Claude Code GrepTool.ts:53](Claude-Code-main/Claude-Code-main/src/tools/GrepTool/GrepTool.ts)
- `read_symbol(mode="list")` — 等价 [Claude Code LSPTool documentSymbol](Claude-Code-main/Claude-Code-main/src/tools/LSPTool/LSPTool.ts)
- `find_symbol_callers` — 等价 [Claude Code LSPTool findReferences](Claude-Code-main/Claude-Code-main/src/tools/LSPTool/LSPTool.ts)
- `smart_search` grep-first — 借鉴 [Claude Code GrepTool 默认行为](Claude-Code-main/Claude-Code-main/src/tools/GrepTool/GrepTool.ts:53)

---

### 2. `nz_coder/runtime_state.py`

Claude Code 风格的 State-as-Message 机制。

```python
@dataclass
class RuntimeState:
    turn_count: int                   # 当前轮次
    max_turns: int                    # 最大轮次
    started_at: float                 # 开始时间
    timeout_seconds: int              # 超时秒数
    last_edit_turn: int               # 最后编辑轮次
    edits_this_run: int               # 本次编辑次数
    has_diff: bool                    # 是否有 diff
    diff_chars: int                   # diff 字符数
    changed_files: list[str]          # 变更文件
    tests_modified: bool              # 是否改了测试
    verification_attempts: int        # 验证尝试次数
    py_compile_ok: bool               # py_compile 是否通过
    broad_test_attempts: int          # broad test 次数
    env_noise_seen: bool              # 是否检测到环境噪音
    searched_patterns: list[str]      # 搜索过的模式
    read_files: list[str]             # 读过的文件
    transition: str                   # 上轮做了什么

    def observe_tool(name, tool_input, output)  # 根据工具调用更新状态
    def build_prompt_block() -> str             # 生成 <system-reminder> 块
```

**设计参考**：
- State 类型 — [Claude Code query.ts:204-217](Claude-Code-main/Claude-Code-main/src/query.ts)
- `<system-reminder>` 注入 — [Claude Code messages.ts:4060-4088](Claude-Code-main/Claude-Code-main/src/utils/messages.ts)
- 递减收益检测 — [Claude Code tokenBudget.ts:45-93](Claude-Code-main/Claude-Code-main/src/query/tokenBudget.ts)

---

## 二、重写文件

### 3. `nz_coder/tools/search.py` — grep_search 重写

**旧版签名**：
```python
def grep_search(pattern, path=".", include=None, max_results=50)
```

**新版签名**：
```python
def grep_search(
    pattern,
    path=".",
    include=None,
    output_mode="files_with_matches",  # 新：默认返回文件路径
    head_limit=None,                    # 新：分页
    offset=0,                           # 新：分页偏移
    context=0,                          # 新：上下文行
    case_insensitive=False,             # 新：大小写不敏感
)
```

**行为变更**：
- 默认 `files_with_matches`（不再默认返回匹配行内容）
- 文件列表按 mtime 排序（最近修改的排前面）
- 三种输出模式：`files_with_matches` / `content` / `count`
- `head_limit` + `offset` 分页
- 返回格式改为结构化：`Found N file(s) matching 'pattern'\nfile1\nfile2...`

---

## 三、修改文件

### 4. `nz_coder/loop.py`

| 行号区域 | 变更 |
|----------|------|
| L12 | 新增 `from nz_coder.runtime_state import RuntimeState` |
| L28 | 新增 `import nz_coder.tools.repo_intel` 触发工具注册 |
| L117 | 新增 `self.runtime_state = RuntimeState()` |
| L176-178 | 新增 `runtime_state.reset(max_turns, timeout_seconds)` |
| L179 | 新增 `config.BLOCK_BROAD_TESTS = False` |
| L193 | 新增 `self.runtime_state.turn_count = turn_index + 1` |
| L206-207 | 构建 system_content 时注入 state_block |
| L395-400 | 工具执行后调用 `self.runtime_state.observe_tool()` |
| L398-399 | 检测到 source diff 后设置 `config.BLOCK_BROAD_TESTS = True` |
| L451-460 | 新增 `_runtime_summary()` 方法 |
| L483-491 | max_turns return 加入 runtime 摘要 |

**关键代码**：
```python
# 每轮注入状态
state_block = self.runtime_state.build_prompt_block()
system_content = self.system_prompt + memory_block + state_block + scratch_block

# 工具后跟踪
self.runtime_state.observe_tool(result_r.name, result_r.tool_input, result_r.output)
if self.runtime_state.has_diff and not config.BLOCK_BROAD_TESTS:
    config.BLOCK_BROAD_TESTS = True  # 阻止后续 broad test
```

---

### 5. `nz_coder/prompt.py`

| 行号 | 变更 |
|------|------|
| L19-20 | 新增搜索行为指令 |
| L37-42 | 更新工具列表（smart_search, read_symbol, find_symbol_callers, grep_search, glob_search, diff_status, verify_changed_files） |

**新增指令**：
```
- For code search, use grep_search or smart_search. NEVER use bash grep/rg.
- grep_search defaults to files_with_matches (sorted by mtime). Use read_symbol
  on the top file before reading it entirely.
```

---

### 6. `nz_coder/tools/bash.py`

| 行号 | 变更 |
|------|------|
| L8 | 新增 `from nz_coder.runtime_state import _is_broad_test_command, _is_exact_test` |
| L138-143 | 新增 broad test 拦截逻辑 |

**拦截逻辑**：
```python
if _is_broad_test_command(command) and getattr(config, "BLOCK_BROAD_TESTS", False):
    return "Error: Broad test runner blocked. A source diff already exists. ..."
```

---

### 7. `nz_coder/swebench/orchestrator.py`

| 行号 | 变更 |
|------|------|
| L251-286 | SWE-bench system prompt 替换为 search-and-verification protocol |

**旧版**：建议 agent "run the most relevant tests you can"

**新版**：9 步硬编码协议
```
1. Start with smart_search
2. Inspect at most 3 candidate files
3. Prefer read_symbol
4. After edit, call diff_status
5. If diff exists, call verify_changed_files
6. Do NOT run pytest/tox
7. py_compile passes → finalize
8. Env noise → stop verifying
9. Non-empty patch > no patch
```

---

## 四、工具总数变化

| 阶段 | 工具数 | 工具列表 |
|------|--------|----------|
| 修改前 | 12 | bash, read_file, write_file, edit_file, apply_patch, replace_lines, python_symbol_check, python_structural_edit, list_directory, grep_search, glob_search, todo |
| 修改后 | 17 | + smart_search, read_symbol, find_symbol_callers, diff_status, verify_changed_files |

> 其他工具（task, save_memory, recall_memory, list_memories, delete_memory, update_scratchpad, read_scratchpad, load_skill, compact）由 subagent, memory, skills, scratchpad 等模块在导入时注册，不计入上述数字。

---

## 五、与 Claude Code 源码的对应关系

| Claude Code 源码位置 | 设计 | NZ-Coder 实现 |
|---------------------|------|---------------|
| `src/tools/GrepTool/GrepTool.ts:53` | 默认 `files_with_matches` | `grep_search` 新版默认 |
| `src/tools/GrepTool/GrepTool.ts:527-553` | mtime 排序 | `grep_search` mtime 排序 |
| `src/tools/LSPTool/LSPTool.ts` | documentSymbol | `read_symbol(mode="list")` |
| `src/tools/LSPTool/LSPTool.ts` | findReferences | `find_symbol_callers` |
| `src/query.ts:204-217` | State 类型 | `RuntimeState` dataclass |
| `src/query.ts:268-279` | 可变 State 跨迭代传递 | `loop.py` 每轮更新 turn_count |
| `src/utils/messages.ts:4060-4088` | `<system-reminder>` 注入 | `RuntimeState.build_prompt_block()` |
| `src/query/tokenBudget.ts:45-93` | 递减收益检测 | 5/8 轮无编辑警告 |
| `src/query/tokenBudget.ts:66-73` | budget continuation nudge | Turn/time 预算提醒 |
| `src/utils/tokenBudget.ts:66-73` | `"Keep working — do not summarize"` | `"Finalize your patch NOW"` |

---

## 六、验证

```bash
python3 -m py_compile nz_coder/tools/repo_intel.py     # ✅
python3 -m py_compile nz_coder/runtime_state.py         # ✅
python3 -m py_compile nz_coder/loop.py                  # ✅
python3 -m py_compile nz_coder/tools/search.py          # ✅
python3 -m py_compile nz_coder/tools/bash.py            # ✅
python3 -m py_compile nz_coder/prompt.py                # ✅

python3 -m pytest tests/ -q                              # ✅ 91 passed
```

**注册验证**：
```
17 tools: apply_patch, bash, diff_status, edit_file, find_symbol_callers,
          glob_search, grep_search, list_directory, python_structural_edit,
          python_symbol_check, read_file, read_symbol, replace_lines,
          smart_search, todo, verify_changed_files, write_file
```

**Smoke tests**：
- ✅ grep_search 三种模式（files_with_matches/content/count）+ mtime 排序 + 分页
- ✅ read_symbol read/list 模式
- ✅ find_symbol_callers 跨文件引用搜索
- ✅ smart_search grep-first 候选文件
- ✅ RuntimeState 6 轮 workflow 状态块递进
- ✅ Broad test blocking（diff 存在后 pytest 被阻止）
- ✅ Env noise 检测 + 警告
- ✅ Diminishing returns 警告
- ✅ Low budget CRITICAL 警告
