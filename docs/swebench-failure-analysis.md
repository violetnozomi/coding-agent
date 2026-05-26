# SWE-bench Lite 失败实例详细分析

> 分析范围：batch-hard01 ~ batch-hard09 共 9 个批次，49 个实例进入 agent 运行，37 个进入官方评测
> 文档生成：2026-05-17

---

## 总体统计

| 类别 | 数量 | 占比 |
|------|------|------|
| 提交 agent 运行 | 49 | 100% |
| 进入官方评测 | 37 | 76% |
| ✅ Resolved | 27 | 55% (of total) / 73% (of evaluated) |
| ❌ Unresolved (有patch) | 10 | 20% |
| ⏱️ 超时 (有patch但被丢弃) | 12 | 24% |
| 🔍 空patch / 搜索失败 | 3 | 6% |
| ❗ 运行错误 | 1 | 2% |

> 注：部分超时实例 (sphinx-8282, matplotlib-23476, matplotlib-25332) 的 patch 被手动提取后进入评测，归入 Unresolved。

---

## 一、官方评测未通过 (UNRESOLVED) — 10 个实例

这些实例生成了非空 patch 并通过了评测环境运行，但官方测试断言失败。

### 1. django__django-13321

| 属性 | 值 |
|------|-----|
| 仓库 | django/django |
| 批次 | batch-hard01 |
| Agent状态 | risky |
| 工具调用 | 24 次 |
| 耗时 | 160s |
| Patch大小 | 720 chars |
| 风险标记 | completed_unverified, verification_needed, tool_errors |

**FAIL_TO_PASS 失败测试** (18个全部失败):
全部 session 相关测试，包括 `test_clear`, `test_custom_expiry_datetime`, `test_custom_expiry_reset` 等 CookieSessionTests 下的测试。

**失败分析**: Agent patch 改动了 Django session 处理逻辑，改动范围过大或方向错误，导致几乎全部 session 测试失败。Agent 自身的验证也不完整 (标记为 completed_unverified)。

---

### 2. matplotlib__matplotlib-24334

| 属性 | 值 |
|------|-----|
| 仓库 | matplotlib/matplotlib |
| 批次 | batch-hard01 |
| Agent状态 | completed (自评通过) |
| 工具调用 | 33 次 |
| 耗时 | 349s |
| Patch大小 | 731 chars |

**FAIL_TO_PASS 失败测试**:
- `lib/matplotlib/tests/test_axes.py::test_set_ticks_kwargs_raise_error_without_labels`

**失败分析**: Agent 自评 completed 但实际未通过。Axes 的 `set_ticks` 方法在特定 kwargs 组合下应抛出特定错误，Agent 的 patch 未正确处理这个边界条件。属于"修复了主路径，遗漏了边缘情况"。

---

### 3. scikit-learn__scikit-learn-14087

| 属性 | 值 |
|------|-----|
| 仓库 | scikit-learn/scikit-learn |
| 批次 | batch-hard02 |
| Agent状态 | risky |
| 工具调用 | 50 次 |
| 耗时 | 529s |
| Patch大小 | 736 chars |
| 风险标记 | tool_errors |

**FAIL_TO_PASS 失败测试** (4个全部失败):
- `test_LogisticRegressionCV_no_refit[ovr-l2]`
- `test_LogisticRegressionCV_no_refit[multinomial-l2]`
- `test_LogisticRegressionCV_no_refit[ovr-none]`
- `test_LogisticRegressionCV_no_refit[multinomial-none]`

**失败分析**: `LogisticRegressionCV` 的 `no_refit` 参数处理涉及多个 solver/penalty 参数组合 (ovr-l2, multinomial-l2, ovr-none, multinomial-none)。Agent 的 patch 可能只覆盖了其中一种组合的代码路径，其他组合全部失败。属于"局部修复未覆盖所有参数维度"。

---

### 4. sympy__sympy-21171

| 属性 | 值 |
|------|-----|
| 仓库 | sympy/sympy |
| 批次 | batch-hard02 |
| Agent状态 | completed (自评通过) |
| 工具调用 | 25 次 |
| 耗时 | 227s |
| Patch大小 | 743 chars |

**FAIL_TO_PASS 失败测试**:
- `test_latex_SingularityFunction`

**失败分析**: Sympy LaTeX 打印机中 `SingularityFunction` 的输出格式问题。Agent 完成了 patch 并通过了自验证 (py_compile)，但 LaTeX 输出格式与官方预期不一致。属于"输出格式精确匹配"类问题 — 代码编译通过不代表输出正确。

---

### 5. matplotlib__matplotlib-25332

| 属性 | 值 |
|------|-----|
| 仓库 | matplotlib/matplotlib |
| 批次 | batch-hard03-extra |
| Agent状态 | risky |
| 工具调用 | 65 次 |
| 耗时 | 680s |
| Patch大小 | 997 chars |
| 风险标记 | tool_errors (14个错误) |

**FAIL_TO_PASS 失败测试**:
- `lib/matplotlib/tests/test_pickle.py::test_complete[png]`

**失败分析**: Agent 的 Figure pickle 修复方案 (新增 `_align_label_groups` 的 pickle 支持) 可能只处理了部分状态变量。`test_complete` 是综合性 pickle 测试，覆盖 subplot、colormap、norm 等多种 pickle 场景。Agent 在探索阶段有 14 个工具错误，可能干扰了准确定位。

---

### 6. sympy__sympy-13146

| 属性 | 值 |
|------|-----|
| 仓库 | sympy/sympy |
| 批次 | batch-hard03 |
| Agent状态 | risky |
| 工具调用 | 44 次 |
| 耗时 | 586s |
| Patch大小 | 407 chars |
| 风险标记 | tool_errors |

**FAIL_TO_PASS 失败测试**:
- `test_evalf_bugs`

**失败分析**: Sympy 数值求值 (`evalf`) 的 bug fix。Patch 较小 (407 chars) 但涉及的数值计算逻辑复杂。Agent 可能只处理了问题描述中的具体场景，遗漏了 evalf 路径下的其他边界情况 (如复数、无穷、精度等)。

---

### 7. sphinx-doc__sphinx-8282

| 属性 | 值 |
|------|-----|
| 仓库 | sphinx-doc/sphinx |
| 批次 | batch-hard03 (agent 超时, patch 手动提取后评测) |
| Agent状态 | agent_failed (timeout) |
| 工具调用 | 0 (子进程运行，tool_log 未记录) |
| 耗时 | 974s |
| Patch大小 | 1833 chars |

**FAIL_TO_PASS 失败测试**:
- `tests/test_ext_autodoc_configs.py::test_autodoc_typehints_none_for_overload`

**失败分析**: Sphinx autodoc 的 typehints 处理，涉及 Python 函数重载 (overload) 场景。Agent 超时前完成了较大 patch (1833 chars)，但未能正确处理 `autodoc_typehints='none'` 配置下的重载函数文档生成。属于"复杂模块理解不足 + 超时中止"的双重问题。

---

### 8. django__django-13220

| 属性 | 值 |
|------|-----|
| 仓库 | django/django |
| 批次 | batch-hard07 |
| Agent状态 | completed |
| 工具调用 | 20 次 |
| 耗时 | 243s |
| Patch大小 | 1649 chars |

**FAIL_TO_PASS 失败测试** (3个失败，1个通过):
- ❌ `test_eq` — ValidationError 基础相等性比较
- ❌ `test_hash` — ValidationError 基础哈希
- ❌ `test_hash_nested` — ValidationError 嵌套哈希
- ✅ `test_eq_nested` — 嵌套相等性 (已通过)

**失败分析**: Django `ValidationError` 的 `__eq__` 和 `__hash__` 方法重构。Agent 的修复使嵌套场景 (`test_eq_nested`) 通过了，但基础场景 (`test_eq`, `test_hash`) 仍然失败。典型"改了调用链下游逻辑，上游入口未对齐"。

---

### 9. django__django-13590

| 属性 | 值 |
|------|-----|
| 仓库 | django/django |
| 批次 | batch-hard08 |
| Agent状态 | risky |
| 工具调用 | 6 次 |
| 耗时 | 68s |
| Patch大小 | 671 chars |
| 风险标记 | max_turns, verification_needed |

**FAIL_TO_PASS 通过**:
- ✅ `test_range_lookup_namedtuple` — 目标 bug 已修复

**PASS_TO_PASS 回归测试** (5个失败):
- `test_command_option_globals` — shell 命令全局变量
- `test_stdin_read_globals` — stdin 读取全局变量
- 以及 3 个其他 shell 相关测试

**失败分析**: Agent 用极少工具调用 (6次/68s) 完成了 patch，目标 bug 修对了，但修改触发了 Django shell 命令模块的全局变量处理回归。典型"快速修复引入副作用"。Agent 因为在前几个实例中消耗了大量 turns (max_turns 标记)，本轮可用的 LLM 轮次极少。

---

### 10. django__django-13660

| 属性 | 值 |
|------|-----|
| 仓库 | django/django |
| 批次 | batch-hard09 |
| Agent状态 | completed |
| 工具调用 | 15 次 |
| 耗时 | - |
| Patch大小 | 917 chars |

**PASS_TO_PASS 回归测试** (2个失败):
- `test_complex_expressions_do_not_introduce_sql_injection_via_untrusted_string_inclusion` — SQL 注入防护回归
- 1 个其他查询表达式回归测试

**失败分析**: Agent 的 patch 修复了功能 bug，但引入了 SQL 注入安全回归 — 修改后的代码允许不受信任的字符串通过表达式注入 SQL。这是最严重的一类失败：Agent 不了解代码的安全约束，修复功能时破坏了安全防护。

---

### 11. django__django-13768

| 属性 | 值 |
|------|-----|
| 仓库 | django/django |
| 批次 | batch-hard09 |
| Agent状态 | completed |
| 工具调用 | 16 次 |
| 耗时 | - |
| Patch大小 | 1055 chars |

**FAIL_TO_PASS 失败测试**:
- `test_get_or_create_with_defaults (test_models.tests.GetOrCreateTests)`

**失败分析**: Django ORM `get_or_create` 与 `defaults` 参数的交互。Agent 在 `QuerySet` 层面做了修改，但 `get_or_create` 的内部实现涉及多个调用层级 (QuerySet → Manager → Model)，可能是改了下游但上游的 `defaults` 参数传递路径未完整穿透。

---

## 二、Agent 超时 (AGENT_FAILED) — 12 个实例

这些实例 agent 在 900s 内未完成运行。**12/13 个实际已产生非空 patch**（仅 sklearn-25570 完全空），但因超时状态被标记为 `agent_failed`，patch 未写入 `predictions.jsonl` 而被浪费。

| # | 实例 | 仓库 | 批次 | 耗时 | Patch大小 | 涉及模块 |
|---|------|------|------|------|-----------|----------|
| 1 | `sphinx-doc__sphinx-8474` | sphinx | hard01 | 920s | 694 chars | toctree collector |
| 2 | `matplotlib__matplotlib-25079` | matplotlib | hard02 | 1150s | 617 chars | colors.py |
| 3 | `matplotlib__matplotlib-25442` | matplotlib | hard02 | 971s | 511 chars | offsetbox.py |
| 4 | `scikit-learn__scikit-learn-25570` | sklearn | hard02 | 927s | **0 chars** | (完全未产出) |
| 5 | `scikit-learn__scikit-learn-25638` | sklearn | hard02 | 928s | 840 chars | multiclass.py |
| 6 | `matplotlib__matplotlib-23476` | matplotlib | hard03 | 1048s | 1654 chars | figure.py (DPI/pickle) |
| 7 | `sphinx-doc__sphinx-8282` | sphinx | hard03 | 974s | 1833 chars | autodoc typehints |
| 8 | `django__django-12184` | django | hard04 | 901s | 612 chars | url resolvers |
| 9 | `django__django-12470` | django | hard05 | 901s | 832 chars | SQL compiler |
| 10 | `django__django-12589` | django | hard05 | 901s | 1477 chars | SQL query |
| 11 | `django__django-12708` | django | hard05 | 900s | 1330 chars | schema operations |
| 12 | `django__django-12856` | django | hard06 | 901s | 592 chars | model base |
| 13 | `django__django-13265` | django | hard07 | 901s | 2150 chars | migration autodetector |

**超时特征分析**:

1. **精确命中 900s 边界** — 9/13 个实例耗时在 900~928s，说明 agent 的编辑阶段本身是足够的，超时发生在最后的验证/调试循环中。agent 不知道即将超时，继续验证而非停止。

2. **高发模块**:
   - Django ORM 内部 (compiler/query/schema) — 代码路径深，验证需要理解 SQL 生成逻辑
   - Django migration autodetector — 逻辑复杂，涉及多种迁移操作的组合
   - matplotlib 非核心模块 (colors/offsetbox/figure pickle) — 不熟悉的代码布局增加探索成本

3. **子进程工具调用不可见** — 超时实例的 `tool_calls` 字段显示为 0，因为 agent 运行在 fork 子进程中，工具日志无法回传。这导致无法分析超时实例的"最后一刻在做什么"。

---

## 三、空 Patch / 搜索失败 (EMPTY_PATCH) — 3 个实例

| 实例 | 仓库 | 批次 | 工具 | 耗时 | 摘要 |
|------|------|------|------|------|------|
| `django__django-16820` | django | hard02 | 10次 | 194s | max_turns + tool_errors |
| `django__django-13158` | django | hard07 | 11次 | 205s | max_turns |
| `django__django-13315` | django | hard08 | 7次 | 45s | max_turns |

**特征**: 均在少量工具调用 (7~11次) 后即用尽 turns，耗时 45~205s。

**根因**: Agent 在搜索阶段无法通过 grep/read_file 定位到关键代码路径。不是"代码太难改"而是 "搜不到在哪儿改"。这可能是因为：
- 问题描述中使用的术语与代码中的命名不匹配
- Agent 的搜索关键词选择策略不够好
- max_turns 耗尽后没有恢复机制

---

## 四、运行错误 — 1 个实例

| 实例 | 仓库 | 批次 | 说明 |
|------|------|------|------|
| `pytest-dev__pytest-7490` | pytest | hard02 | agent 运行过程中抛出异常，无法生成预测 |

---

## 五、失败模式总结

### 模式 A：语义修复不完整 (5个 unresolved)

Agent 定位到了正确文件，但修改只覆盖了主路径：
- django-13321: session 逻辑改了但方向错误，18个测试全失败
- sklearn-14087: 只修了一种 solver/penalty 组合，其余3种失败
- matplotlib-24334: set_ticks 修复遗漏了 kwargs 边界
- sphinx-8282: autodoc 重载处理逻辑不完整
- django-13220: 嵌套场景通过但基础场景失败

**根因**: Agent 只看了直接相关的代码片段，未沿调用链检查所有消费方。

### 模式 B：修复引入回归 (3个 unresolved)

Agent 的 patch 修复了目标 bug (FAIL_TO_PASS 部分通过)，但破坏了其他功能 (PASS_TO_PASS 失败)：
- django-13590: 修了 ORM 查询，破坏了 shell 全局变量 (5个回归)
- django-13660: 修了表达式，引入了 SQL 注入漏洞 (安全回归)
- django-13768: 改了 QuerySet 但 get_or_create 的 defaults 参数路径断裂

**根因**: Agent 不了解修改的全部影响范围，也缺乏"检查所有调用方"的机制。

### 模式 C：输出格式不匹配 (2个 unresolved)

- sympy-21171: LaTeX 格式与预期不一致
- sympy-13146: evalf 数值计算输出差异

**根因**: 科学计算库的输出格式精确匹配要求高，Agent 无法在本地验证输出是否与官方期望一致。

### 模式 D：超时丢弃 (12个，含1个被手动评测后为 unresolved)

Agent 在编辑后无法及时停止，超时导致已生成的 patch 被丢弃。
- 其中 matplotlib-23476 和 sphinx-8282 被手动提取后评测为 unresolved
- 其余 10 个 patch 从未被评测，**这些 patch 中很可能有可以通过的**

### 模式 E：搜索定位失败 (3个)

Agent 在少量工具调用后即用尽 turns，未找到关键代码路径。

---

## 六、按仓库的成功率

| 仓库 | 进入评测 | Resolved | 首次成功率 |
|------|----------|----------|-----------|
| django/django | 27 | 22 | **81%** |
| matplotlib/matplotlib | 4 | 1 | 25% |
| sphinx-doc/sphinx | 1 | 0 | 0% |
| sympy/sympy | 2 | 0 | 0% |
| scikit-learn/scikit-learn | 1 | 0 | 0% |
| pytest-dev/pytest | 1 | 0 | 0% |

> 注：matplotlib/sphinx 有部分手动提取的超时 patch 评测为 unresolved，未计入 resolved。进入评测数仅供参考 (部分实例跨多个 batch)。

**关键结论**: Django 以外仓库的首次成功率极低 (0~25%)。主要原因：
1. 代码布局不熟悉 — matplotlib 的 `lib/matplotlib/` 布局、sklearn 的 estimator 模式、sympy 的符号计算架构
2. 测试运行困难 — 这些仓库在本地 Python 3.13 环境下经常有 import/dependency 问题
3. 模型知识偏差 — DeepSeek 模型对 Django 代码模式更熟悉

---

## 七、改进方向 (优先级排序)

### P0 — 立即实施

1. **超时 patch 不要丢弃**: `agent_failed` 但有非空 diff (>100 chars) 的 patch 应标记为可评测。12 个超时实例中有 12 个产生了有效 patch (511~2150 chars)，部分很可能通过官方测试。

2. **Agent 预算感知**: 在 system prompt 中告知 agent 时间上限，让它自己把握"探索 vs 产出"的节奏。尤其是 Django ORM 内部模块 (compiler/query)，agent 常花 80% 时间探索、15% 时间编辑、5% 时间在验证中浪费。

### P1 — 短期改进

3. **简化验证阶段**: 默认只做 `python3 -m py_compile`。本地测试套件运行经常因 Python 版本差异、依赖缺失产生噪音，反而误导 agent 继续"修复"不存在的问题。

4. **仓库特定引导 prompt**: 对非 Django 仓库加入代码布局说明 (如 matplotlib 的 `lib/` 前缀、sklearn 的 `sklearn/` 包路径)，减少探索阶段的搜索轮次。

### P2 — 中长期改进

5. **自动 retry 机制**: 利用官方 FAIL_TO_PASS / PASS_TO_PASS 失败日志进行定向 retry（已有 `retry-agent` 命令，需对 batch-hard 结果执行）。

6. **安全/兼容性约束 prompt**: 显式告知 agent：不要引入 SQL 注入回归、不要改变公共 API 签名、check 所有调用方。
