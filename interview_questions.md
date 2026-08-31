# nz_coder Coding Agent 深度面试题

> 使用方式：把本文档 + 对应模块的源码一起发给 Codex，让它逐题作答。
> 每个问题都要求结合具体代码回答，不接受泛泛而谈。

---

## 一、架构总览（10 题）

### 1.1 请用一句话描述 nz_coder 的定位，然后画出核心模块的依赖关系图。

追问：哪些模块是通过依赖注入连接的？哪些是通过模块级全局单例？为什么没有统一成一种方式？

### 1.2 你的 agent 主循环是什么范式？跟经典 ReAct 的区别在哪里？

追问：你加了 planning/replan 之后，还算 ReAct 吗？学术上应该叫什么？

### 1.3 解释"三层记忆架构"：Scratchpad、Memory、Context 分别是什么？

追问：如果把 Scratchpad 也持久化到磁盘，会有什么问题？为什么选择不持久化？

### 1.4 什么是 state-as-message？你的系统里哪些地方用了这个模式？

追问：如果不注入 RuntimeState，让模型自己从对话历史中推断当前状态，会发生什么？在 SWE-bench 上大概会掉多少分？

### 1.5 你的系统有几种退出状态？每种状态分别在什么条件下触发？

追问：completed 和 completed_unverified 的区别是什么？什么情况下会出现 completed_unverified？

### 1.6 解释上下文四层成本模型：固定层、半固定层、任务层、动态层分别对应你代码里的什么？

追问：为什么 memory_block 放在 stable_system 里而不是 dynamic_context 里？这跟 prompt caching 有什么关系？

### 1.7 你的 agent 跑一个任务平均消耗多少 token？最大的 token 消耗来源是什么？

追问：如果要把 token 消耗降低 50%，你会从哪里入手？

### 1.8 你的系统支持哪些 LLM provider？切换 provider 需要改什么？

追问：reasoning_content 字段的处理为什么需要 PASS_REASONING_CONTENT 配置？哪些 provider 需要它？

### 1.9 画出一次完整的工具调用生命周期：从 LLM 返回 tool_calls 到 tool result 注入 messages。

追问：如果 LLM 一次返回 5 个 tool_calls，其中 3 个是读工具、2 个是写工具，执行顺序是什么？

### 1.10 你的项目对标 Claude Code，跟 Claude Code 相比最大的差距是什么？

---

## 二、Agent 主循环 loop.py（12 题）

### 2.1 run() 方法为什么拆成 30 行骨架 + 十几个小方法？拆之前是什么样的？

追问：如果要新增一个"工具调用后自动保存 checkpoint"的功能，你需要改哪些方法？

### 2.2 _build_context_layers 的预算守卫是怎么工作的？如果总 token 超限，截断顺序是什么？

追问：为什么先砍 scratch，再砍 memory，最后砍 state？能不能反过来？

### 2.3 _inject_dynamic_context 把动态内容放到首条 user 消息而非 system prompt，为什么？

追问：如果对话历史里没有 user 消息（理论上不应该，但防御性编程），这个函数怎么处理？

### 2.4 _sanitize_messages 做了六步清洗，哪一步最容易出 bug？

追问：如果不合并连续 user 消息，哪些 provider 会报错？为什么 OpenAI 不报错但 Bedrock 会？

### 2.5 _inject_missing_tool_results 解决什么问题？什么场景下会出现"孤立的 tool_calls"？

追问：如果不修复孤立 tool_calls，API 会返回什么错误？

### 2.6 解释 LLMResult dataclass 的三种状态：正常完成、客户端错误、不可恢复错误。

追问：为什么 400/422 错误要注入诊断让模型自我修正，而不是在代码层面修复 JSON？

### 2.7 _execute_concurrent 的并发策略是什么？为什么 max_workers 限制为 4？

追问：task 工具为什么在 non_concurrent_tools 集合里？如果把它并发执行会怎样？

### 2.8 事务管理（txn.begin / commit / rollback）在什么条件下触发？

追问：bash 测试失败（command_failed=True）为什么不触发事务回滚？

### 2.9 _maybe_save_learnings 为什么只在 auto 模式下执行？

追问：如果在 default 模式下也自动提取经验，会有什么安全或用户体验问题？

### 2.10 micro_compact 和 auto_compact 的触发时机有什么区别？

追问：如果一个对话有 100 轮工具调用，micro_compact 大概在第几轮开始生效？

### 2.11 verification gate 的工作流程是什么？make_gate_message 返回什么内容？

追问：MAX_VERIFICATION_GATE_PROMPTS=2 意味着什么？如果设为 0 会怎样？

### 2.12 _parse_turn_budget 支持哪些格式？"+50k"为什么映射到固定的 200 turns 而非 50000？

---

## 三、运行时状态 runtime_state.py（8 题）

### 3.1 observe_tool 为什么要解析 diff_status 的文本输出？为什么不直接返回结构化 dict？

追问：_parse_changed_files 的解析逻辑有哪些边界条件？什么格式的 diff_status 输出会解析错误？

### 3.2 task_complexity() 的 L0~L3 阈值是怎么定的？有没有数据支撑？

追问：L1 的 diff_chars <= 1200 是什么概念？大约对应多少行代码的改动？

### 3.3 build_prompt_block 的 reminder 有 8 种类型，它们之间有没有冲突或重复的可能？

追问：如果同时满足"空转 8 轮"和"Low budget 5 turns remaining"，两条 reminder 都会出现，模型会不会被矛盾信息困扰？

### 3.4 acceptance_criteria 的提取是纯启发式的，准确率大概多少？

追问：对于"Fix the bug where timezone-aware datetimes are incorrectly compared"这句话，extract_acceptance_criteria 能提取出什么？

### 3.5 环境噪音检测（ENV_NOISE_PATTERNS）覆盖了哪些情况？有没有漏掉的？

追问：如果用户的代码里真的有 "No module named mypackage" 的错误（不是环境问题），会被误判吗？

### 3.6 RuntimeState 的持久化为什么用 JSON 而非 pickle？

追问：restore() 用 setattr 逐字段恢复，如果磁盘上的 JSON 包含 RuntimeState 没有的字段（比如旧版本遗留），会怎样？

### 3.7 _is_broad_test_command 和 _is_exact_test_command 的判断逻辑有没有灰色地带？

追问："pytest tests/" 算 broad，"pytest tests/test_foo.py" 算 exact——那 "pytest tests/unit/" 呢？

### 3.8 wants_tests 字段怎么影响 build_prompt_block 的 reminder 内容？

追问：如果用户说"fix the bug and add a regression test"，task_mode 和 wants_tests 分别是什么？

---

## 四、记忆系统 memory.py + scratchpad.py（10 题）

### 4.1 recall() 的多信号评分公式是什么？每个信号的权重为什么这样设？

追问：freshness 权重从旧版的 0.3 降到 0.1，解决了什么问题？

### 4.2 _tokenize 的"代码感知"体现在哪里？跟普通的 split by whitespace 有什么区别？

追问：对 "parse_http_date" 这个 token，_tokenize 会输出什么？

### 4.3 save() 的去重合并流程有几步？_SIMILARITY_MERGE_THRESHOLD=0.72 是怎么调出来的？

追问：如果两条 memory 的 description 完全不同但 content 几乎相同，会合并吗？

### 4.4 build_prompt_block 为什么给 user 类型 memory 始终保留前 3 个 slot？

追问：如果有 10 条 user 类型 memory 但只有 3 个 slot，按什么顺序排？

### 4.5 extract_session_learnings 的规则提取能捕获哪些模式？LLM 提取能额外捕获什么？

追问：如果用户说"this project uses Django 3.2, don't use async views"，规则提取和 LLM 提取分别能提取出什么？

### 4.6 rerank_memories 的 LLM rerank 失败时怎么降级？为什么不直接报错？

追问：rerank 的 LLM 调用大约消耗多少 token？每次 run() 调几次？

### 4.7 Scratchpad 的 replace_category 为什么要单独做？直接删旧 + update 新的不行吗？

追问：plan 的 _MAX_PLAN_CHARS=2000 但 build_prompt_block 的 plan_budget 只有 1200，多出的 800 字符去哪了？

### 4.8 build_prompt_block 为什么用倒序选取 other_entries（最新的优先）？

追问：如果 agent 在第 1 轮记录了一个关键的 finding，到第 15 轮时还能看到吗？

### 4.9 _memory_similarity 用 max(jaccard, min_coverage) 有什么问题？_SIMILARITY_MIN_TOKENS_FOR_MERGE=5 解决了什么？

追问：一条只有 "use pytest" 两个 token 的 memory，在加 min_tokens 之前会发生什么？

### 4.10 Memory 的 cleanup() 方法有什么触发条件？谁来调用它？

追问：如果 memory 条目增长到 200 条，MEMORY.md 的行数/字节保护是怎么工作的？

---

## 五、权限与安全 permissions.py + command_policy.py（6 题）

### 5.1 画出 check() 方法的完整决策树：一个 bash 命令从进入到返回 allow/deny/ask 的每一步。

追问：如果用户在 settings.json 里同时配了 allow: ["bash"] 和 deny: ["bash(prefix:rm)"]，执行 "rm -rf /" 时是什么结果？

### 5.2 classify_bash 的 _DANGEROUS_PATTERNS 和 _MUTATING_PATTERNS 有什么区别？

追问：为什么 "sudo apt-get install" 是 dangerous 但 "pip install requests" 只是 mutating？

### 5.3 acceptEdits 模式的设计意图是什么？跟 auto 模式的区别在哪里？

追问：在 acceptEdits 模式下，"git push origin main" 需要用户确认吗？

### 5.4 is_known_read_only_command 的白名单覆盖了哪些命令？漏掉了哪些常见的安全命令？

追问："python3 -c 'print(1)'" 算 read-only 吗？你的系统怎么判断的？

### 5.5 PermissionRule 的 prefix matcher 是怎么工作的？有没有注入风险？

追问：如果用户给了 allow: ["bash(prefix:git )"]，那 "git push --force" 会被允许吗？这安全吗？

### 5.6 ask_user 的 p=always-prefix 功能有什么用户体验价值？

追问：如果用户连续 approve 了 "git status"、"git diff"、"git log"，能不能自动学会 "git " 前缀是安全的？

---

## 六、仓库智能工具 repo_intel.py（8 题）

### 6.1 smart_search 的评分为什么用 TF-IDF 而非简单的关键词计数？

追问：一个 3000 行的文件出现了 "parse" 50 次 vs 一个 100 行的文件出现了 "parse" 5 次，分数会怎样比较？

### 6.2 _file_weight 给测试文件 0.45 的权重，给 docs 0.35，这些数字是怎么定的？

追问：在 SWE-bench 上，有没有因为测试文件权重太低导致漏掉关键测试文件的情况？

### 6.3 verify_changed_files 对 Go 项目跑的是 go test 而非 go vet，这个选择对吗？

追问：go test 会执行测试用例，如果测试依赖数据库连接，会发生什么？跟 py_compile 是同一级别的检查吗？

### 6.4 read_symbol 的 _collect_symbols 支持嵌套到多深？有没有递归深度限制？

追问：对一个自动生成的 protobuf Python 文件（可能有数百个嵌套 message 类），会不会有性能问题？

### 6.5 find_symbol_callers 用 NodeVisitor 而非 ast.walk，有什么好处？

追问：旧版用 ast.walk 时，"obj.foo()" 为什么会产生重复报告？NodeVisitor 是怎么解决的？

### 6.6 diff_status 的 recommendation 是怎么生成的？在什么情况下会给出错误的建议？

追问：如果用户的任务是"删除一个已废弃的函数"，diff 只包含删除操作，recommendation 会说什么？

### 6.7 _git_grep_pathspecs 为什么需要处理 base 是文件还是目录的情况？

追问：如果 path 参数传了一个不存在的目录，smart_search 会怎样？

### 6.8 smart_search 的 _extract_tokens 为什么要从引号字符串里额外提取关键词？

追问：对 traceback 里的 'KeyError: "user_id"'，_extract_tokens 能提取出什么？

---

## 七、子 Agent subagent.py（6 题）

### 7.1 子 agent 为什么不能用父 agent 的 messages 列表？隔离带来了什么好处和代价？

追问：如果父 agent 已经花了 10 轮找到了 bug 所在的文件，子 agent 怎么知道这个信息？

### 7.2 _parent_context_block 读取了哪些父 agent 状态？有什么信息丢失？

追问：如果父 agent 的 scratchpad 里有 5 条 failure 记录但 content 加起来超过 2000 字符，截断后子 agent 会丢失什么？

### 7.3 四种 agent_type 的工具集差异是什么？为什么 review 和 explore 都是 read-only？

追问：review 类型如果需要运行 "git diff --stat"，bash 的 read_only 怎么判断这个命令？

### 7.4 _completion_with_timeout 为什么需要 SIGALRM 和 ThreadPoolExecutor 两种超时机制？

追问：在 macOS 和 Linux 上行为有没有区别？Windows 上呢？

### 7.5 general 模式下子 agent 完成后为什么自动跑 verify_changed_files？

追问：如果 verify 失败了，rollback 会回滚哪些文件？子 agent 写了 3 个文件但只有最后一个导致 verify 失败，能不能只回滚最后一个？

### 7.6 子 agent 的 scratchpad 文件（scratch-xxx.md）什么时候清理？有没有泄漏风险？

追问：如果 agent 跑了 100 次 subagent，会产生 100 个 scratch 文件，磁盘占用怎么控制？

---

## 八、上下文管理 context.py（6 题）

### 8.1 estimate_tokens 的 CJK 修正公式是什么？修正后误差大约多少？

追问：对一段纯中文文本（比如 1000 个中文字符），修正前和修正后分别估算多少 token？实际应该是多少？

### 8.2 micro_compact 为什么保护含 traceback/FAILURES 的结果不被压缩？

追问：如果一个 50KB 的 tool result 里有 traceback 但大部分内容是无关的日志，会被压缩吗？

### 8.3 time-based compact 的 30 分钟阈值是怎么定的？跟 server-side prompt cache 有什么关系？

追问：如果 provider 的 cache TTL 是 5 分钟（而非 30 分钟），这个阈值应该怎么调？

### 8.4 auto_compact 的 LLM 摘要为什么要注入 git diff --stat？

追问：如果 compact 发生在 agent 做了 5 次编辑之后，摘要丢失了"哪个文件改了什么"的信息，后续 agent 会怎样？

### 8.5 persist_large_output 的 TRIGGER_CHARS=30000 是怎么定的？

追问：如果 grep_search 返回了 40000 字符的结果，preview 只保留 2000 字符，剩下的 38000 字符怎么被 agent 使用？

### 8.6 _COMPACT_BUDGET=80000 字符的含义是什么？为什么从尾部截取而非均匀采样？

---

## 九、Planning 与 Replanning（8 题）

### 9.1 _maybe_generate_plan 的触发条件是什么？为什么 bugfix 模式默认不触发？

追问：一个复杂的 bugfix（涉及 5 个文件的级联修改）应不应该触发 planning？当前设计会怎样？

### 9.2 estimate_text_complexity 的打分逻辑是什么？每个信号的权重合理吗？

追问：用户说"重构 auth 模块"只有 6 个字，但实际是个巨大的任务，estimate_text_complexity 会返回什么？

### 9.3 planning prompt 为什么要求"最后一步必须是 verification"？

追问：如果模型的 plan 最后一步不是 verification，planning 代码会怎么处理？（答案：不会处理——这是一个 prompt-level 约束，不是代码级强制）

### 9.4 replan 的三个触发条件分别对应什么实际场景？

追问：条件 3（复杂度升级）把 initial_plan_complexity（simple/moderate/complex）跟 task_complexity（L0~L3）混合比较，这两个维度一样吗？

### 9.5 hydrate 机制（中断恢复时把 plan 从 RuntimeState 补回 scratchpad）为什么是必要的？

追问：如果不做 hydrate，恢复后 agent 还能看到 plan 吗？（提示：scratchpad 不持久化，RuntimeState 持久化）

### 9.6 PLANNING_ENABLED 默认 False 的决策理由是什么？

追问：如果默认 True，现有的哪些测试会失败？（提示：fake client 不会预期 planning 的额外 LLM 调用）

### 9.7 plan 写入 scratchpad 时用 replace_category 而非 update，为什么？

追问：如果用 update，连续 replan 两次后 scratchpad 里会有几条 plan？build_prompt_block 会展示哪条？

### 9.8 _call_planning_llm 和 _call_replan_llm 都不传 tools 参数，为什么？

追问：如果给 planning LLM 也配上工具（比如让它先 grep 一下再制定计划），这个架构改动大吗？值得做吗？

---

## 十、设计决策与取舍（10 题）

### 10.1 你的系统用 Jaccard + TF-IDF 做代码检索而非向量数据库，为什么？什么情况下应该切到向量？

### 10.2 验证体系从 Python-only 扩展到多语言，最大的技术挑战是什么？

追问：verify_changed_files 对 JS/TS 项目找不到 typecheck 命令时返回 WARN，这会不会导致 verification gate 永远通过？

### 10.3 子 agent 的事务管理（txn）是独立的，这意味着父子之间的文件修改不在同一个事务里。这安全吗？

追问：如果父 agent 和子 agent 同时修改同一个文件会发生什么？

### 10.4 _is_client_error 用字符串匹配 "400"、"422" 作为 fallback，这有没有误判风险？

追问：如果错误信息是 "Rate limit exceeded (HTTP 400)"，这是应该重试还是注入诊断？

### 10.5 你的系统没有用 structured output / JSON mode 来解析 LLM 的 plan 和 rerank 输出，为什么？

追问：_create_chat_completion 的 response_format fallback 机制是怎么工作的？

### 10.6 auto_compact 的摘要质量完全依赖 LLM，如果摘要质量差（丢失关键文件路径），后果是什么？

追问：有没有办法验证摘要质量？比如检查摘要中是否包含 changed_files 里的所有路径？

### 10.7 config.BLOCK_BROAD_TESTS 是一个全局可变状态，在 run() 中被修改。这个设计有什么风险？

追问：如果两个 AgentLoop 实例共享同一个 config 模块（比如多线程场景），会发生什么？

### 10.8 你的系统的错误恢复策略是什么？连续失败多少次会放弃？

追问：recovery.backoff_wait() 的退避策略是什么？指数退避？固定间隔？

### 10.9 整个项目最大的技术债务是什么？如果只能花一天时间改进一个地方，你会选哪里？

### 10.10 如果要把这个 agent 从单用户本地工具变成多租户云服务，最大的架构挑战是什么？

追问：config.WORKDIR 是模块级变量，多租户场景下怎么隔离？

---

## 十一、场景题（8 题）

### 11.1 用户说"帮我把这个 Express 项目从 JavaScript 迁移到 TypeScript"。描述 agent 从收到消息到完成任务的全过程。

追问：task_mode 是什么？会触发 planning 吗？verify_changed_files 会跑什么检查？

### 11.2 agent 在第 8 轮做了一个编辑，第 9 轮跑 pytest 失败，第 10~15 轮一直在读文件和 grep 没有再编辑。RuntimeState 的 reminder 会怎么变化？

### 11.3 agent 跑到第 45 轮（max_turns=50），有 diff，py_compile 通过了，但模型还在尝试跑 broad test。系统会怎么处理？

### 11.4 网络不稳定，LLM API 连续返回 3 次 502，然后第 4 次返回 400（JSON 格式错误）。系统的处理流程是什么？

### 11.5 用户上传了一个 10 万行的日志文件，说"帮我找出 OOM 的原因"。上下文管理系统会怎么处理？

### 11.6 agent 在执行 write_file 后被 SIGKILL 杀死。下次启动时发生什么？

追问：_inject_missing_tool_results 会注入什么？RuntimeState.load 会恢复什么？文件系统上已经写入的内容怎么办？

### 11.7 两个用户同时给 agent 发消息（假设未来支持并发）。当前架构的哪些部分会出问题？

### 11.8 用户说"remember that this project uses Poetry instead of pip"。这句话会触发什么？下次 session 怎么生效？

---

## 使用说明

1. 把本文档发给 Codex，同时附上对应模块的源码
2. 要求 Codex 逐题回答，每题引用具体的函数名、变量名和行号
3. 对于追问，要求 Codex 像面试一样深入解答，不能只说"是"或"不是"
4. 回答完后，标注你觉得自己代码里需要改进的地方
