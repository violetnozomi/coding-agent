# 提示词：为 nz_coder Coding Agent 生成完整架构学习文档

## 你的角色

你是一位资深软件架构师和技术作家。我需要你为我的 coding agent 项目（nz_coder）编写一份**面向开发者学习的架构文档**。这份文档的读者是我自己——我理解 AI agent 的基本概念，但这个项目的代码是 AI 帮我写的，我需要通过这份文档**真正理解每一个模块的设计思路、实现细节和决策原因**。

## 文档目标

不是 API 参考文档，不是用户手册，而是一份**"为什么这样做"的架构解说**。对每个模块，我需要理解：

1. **它解决什么问题**（没有它会怎样）
2. **它的核心设计思路**（用了什么模式/策略，为什么选这个而非其他）
3. **关键实现细节**（核心数据结构、算法、边界条件处理）
4. **它如何与其他模块交互**（数据流向、调用关系、依赖注入方式）
5. **已知的设计取舍和局限**（为什么做了某个妥协，什么场景下会出问题）

## 文档结构要求

按以下大纲组织，每一章节都要有**具体的代码引用**（指出关键函数/类/变量名）和**类比解释**（用日常概念辅助理解技术决策）。

### 第一部分：全局架构总览

1. **系统定位**：nz_coder 是什么，对标什么产品（Claude Code），适用什么场景（SWE-bench + 交互式开发）
2. **核心循环**：一次完整的用户请求到 agent 完成任务的全流程，画出数据流
3. **模块地图**：所有模块的一句话定位 + 它们之间的依赖关系图
4. **三层记忆架构**：Scratchpad / Memory / Context 各自的生命周期、存储位置、注入方式
5. **关键设计原则**：这个项目反复出现的设计模式（state-as-message、graceful degradation、deny-first security 等）

### 第二部分：逐模块深度解说

对以下每个模块，按上面的五个维度展开：

**A. loop.py — Agent 主循环**
- run() 的瘦循环设计：为什么把 250 行拆成 30 行骨架 + 小方法
- 上下文分层构建：_build_context_layers 为什么要分稳定前缀和动态注入，跟 prompt caching 的关系
- 工具执行的并发策略：为什么读工具并发、写工具串行，non_concurrent_tools 集合的作用
- 四个退出路径统一到 _finalize 的设计
- LLMResult dataclass 替代 tuple 的动机
- 400/422 错误注入诊断 vs 5xx 重试的分流逻辑
- _sanitize_messages 的六步清洗流程：每一步解决什么 API 兼容性问题

**B. runtime_state.py — 运行时状态跟踪**
- state-as-message 的核心思想：为什么不让模型自己记状态，而是系统每轮注入客观事实
- build_prompt_block 的 reminder 生成逻辑：每条 reminder 的触发条件和优先级
- task_complexity 分级：L0~L3 的阈值是怎么定的，跟渐进式规范的关系
- 空转检测：no_edit_turns 的计算逻辑，为什么 discuss 模式要跳过
- observe_tool 的分发逻辑：为什么要解析 diff_status 的文本输出而非结构化返回
- 持久化/恢复：to_dict/restore/save/load 的设计，为什么用 JSON 而非 pickle
- acceptance_criteria 提取：extract_acceptance_criteria 的启发式规则

**C. context.py — 上下文压缩**
- 为什么需要三种压缩策略：micro_compact / time-based compact / auto_compact
- micro_compact 的优先级：为什么按大小降序压缩，为什么保留含 traceback 的结果
- estimate_tokens 的 CJK 修正：为什么 len(json)//4 在中文场景下高估
- persist_large_output 的阈值设计：TRIGGER_CHARS 和 PREVIEW_CHARS 的取舍
- auto_compact 的 LLM 摘要策略：为什么选择从尾部截取而非全量摘要

**D. memory.py — 持久化记忆**
- 三层记忆架构中的定位：跟 Scratchpad（短期）和 Context（当前）的区别
- recall 的多信号评分：coverage / jaccard / exact / freshness 四个权重的设计意图
- _tokenize 的代码感知设计：为什么要拆 snake_case/camelCase，为什么要做词干还原
- save 的去重合并流程：_find_merge_target → _merge_memory 的判断链
- build_prompt_block 的用户偏好优先注入：为什么 user 类型 memory 始终占前 3 个 slot
- extract_session_learnings 的双通道设计：规则提取 + LLM 提取，为什么 LLM 失败要 fallback
- rerank_memories 的 LLM rerank：为什么是可选的，失败时如何保持原排序

**E. scratchpad.py — Session 内工作记忆**
- 为什么需要 scratchpad：跟 RuntimeState 的区分（主观推理 vs 客观事实）
- category 枚举设计：hypothesis/attempt/failure/finding/plan 各自的使用场景
- replace_category 的设计动机：为什么 plan 需要专用方法而非普通 update
- build_prompt_block 的 plan 优先策略：为什么给 plan 分配 1200 字符的独立 budget
- 不持久化的设计决策：为什么每次 run() 开始时 clear()

**F. permissions.py — 权限系统**
- deny → mode → allow → ask 四层 pipeline 的设计：为什么 deny 最高优先级
- 四种模式（default/auto/plan/acceptEdits）各自的适用场景
- bash 命令分类的保守策略：classify_bash 的 dangerous/mutating/read-only 三级
- PermissionRule 的 content matcher：prefix:git 这种规则怎么工作的
- ask_user 的交互式确认：a=always / p=always-prefix 的运行时规则动态添加

**G. repo_intel.py — 仓库智能工具集**
- 五个工具的定位：diff_status / verify_changed_files / read_symbol / smart_search / find_symbol_callers
- smart_search 的 grep-first + TF-IDF 评分：为什么先用 git grep 缩小范围再精排
- read_symbol 的 AST 递归遍历：_collect_symbols 为什么要处理嵌套类和内部函数
- verify_changed_files 的语言无关化：怎么按语言选择不同的 lint 工具
- find_symbol_callers 的 NodeVisitor 设计：为什么用 visitor 而非 ast.walk，怎么避免重复报告

**H. subagent.py — 子 Agent**
- 隔离优先的设计：为什么子 agent 有独立的 messages、tools、TransactionManager
- 四种类型（explore/review/test/general）的工具集差异
- 父子上下文传递：_parent_context_block 读取 RuntimeState + scratchpad 的设计
- 超时保护：signal.SIGALRM + ThreadPoolExecutor 的双重机制，为什么需要两种
- 结果验证：general 模式下为什么自动跑 verify_changed_files 再决定 commit/rollback

**I. verification.py — 验证状态管理**
- verification gate 的核心概念：为什么模型想结束时要拦住它验证
- _is_verification_command 的启发式：覆盖了哪些语言的测试/编译命令
- 环境噪音过滤：_is_env_import_error 为什么要区分"环境问题"和"代码缺陷"
- scratch file 写入不重置 gate：_is_scratch_file_write 的设计意图

**J. task_policy.py — 任务策略**
- 语言无关的文件分类：is_source_file / is_test_file / language_for_path
- detect_task_mode 的优先级：test > refactor > feature > bugfix > discuss > general
- estimate_text_complexity 的纯文本启发式：为什么 planning 前不能用 task_complexity()
- is_broad_test_command vs is_exact_test_command 的判断逻辑

**K. Planning + Replanning — 规划层**
- 为什么需要 planning：串行 ReAct 的"走一步看一步"在复杂任务上的局限
- 触发条件的设计：为什么用 task_mode + text_complexity 双条件而非单一条件
- planning prompt 的结构：为什么限制 5 步、要求最后一步必须是验证
- replan 的三个触发条件：空转 / 验证失败 / 复杂度升级，各自对应什么场景
- hydrate 机制：中断恢复时为什么要从 RuntimeState 把 plan 补回 scratchpad
- PLANNING_ENABLED 默认关闭的测试兼容性考量

### 第三部分：数据流与生命周期

1. **一次完整 run() 的时序图**：从 _init_run 到 _finalize，标注每个阶段的状态变化
2. **上下文注入的完整流程**：system_prompt → memory_block → state_block → scratch_block → stable/dynamic 分离 → API 调用
3. **工具调用的完整流程**：LLM 返回 tool_calls → dispatch → execute → observe → persist → diagnostic
4. **记忆的生命周期**：用户输入 → session 内 scratchpad → session 结束 extract_session_learnings → 持久化 memory → 下次 session build_prompt_block 注入
5. **中断恢复的完整流程**：timeout kill → 下次 run() → _inject_missing_tool_results → RuntimeState.load → scratchpad hydrate → 从 start_turn 继续

### 第四部分：设计决策速查表

用表格形式列出关键设计决策：

| 决策 | 选择 | 替代方案 | 选择原因 |
|------|------|----------|----------|
| 例：检索方式 | Jaccard + TF-IDF | 向量数据库 | 零依赖、实时更新、代码场景精确匹配更重要 |
| ... | ... | ... | ... |

覆盖至少 20 个关键决策。

### 第五部分：术语表

列出项目中频繁出现的术语和缩写：verification gate、micro_compact、state-as-message、hydrate、broad test、env noise、scratch file 等，每个给一句话定义。

## 写作风格要求

- **每个"为什么"都要回答**：不能只说"它做了 X"，必须说"它做了 X 是因为 Y，如果不做 X 会导致 Z"
- **用类比辅助理解**：比如 "verification gate 就像论文提交前的审稿环节——你可以写完就提交，但系统会拦住你先跑一遍 checker"
- **引用具体代码**：提到某个逻辑时，说明在哪个文件的哪个函数里，关键变量叫什么
- **画出关系图**：用 Mermaid 或 ASCII art 画出模块依赖图、数据流图、状态机图
- **标注已知局限**：每个模块结尾列出"当前局限与未来改进方向"
- **中文为主**，代码标识符和英文术语保持原样不翻译
- **总长度不少于 15000 字**，确保每个模块都有足够深度

## 我会提供的代码文件

我会把以下文件的完整源码作为输入：

1. config.py — 配置管理
2. loop.py — Agent 主循环
3. runtime_state.py — 运行时状态
4. context.py — 上下文压缩
5. memory.py — 持久化记忆
6. scratchpad.py — 工作记忆
7. permissions.py — 权限系统
8. command_policy.py — Shell 命令安全分类
9. repo_intel.py — 仓库智能工具
10. subagent.py — 子 Agent
11. verification.py — 验证状态管理
12. task_policy.py — 任务策略
13. tool_executor.py — 工具执行器
14. transaction.py — 事务管理
15. recovery.py — 错误恢复
16. trace.py — 追踪记录
17. changes.py — 变更跟踪

请基于这些代码文件，按上述大纲生成完整的架构学习文档。如果某个模块的代码我没有提供，在文档中标注"[代码未提供，基于接口推断]"并根据其他模块的调用方式推断其行为。
