# Mew Hook Config Audit

本文档基于 `mewcode` 源码与测试，整理其 hook 配置文件可写的内容、真实生效的触发点，以及和当前 `NZ-Coder` 的差异。

## 1. 配置入口

`mew` 的 hook 配置不是单独文件，而是挂在主配置里的 `hooks:` 字段。

配置加载顺序见 `mewcode/mewcode/config.py`：

1. `~/.mewcode/config.yaml`
2. `<cwd>/.mewcode/config.yaml`
3. `<cwd>/.mewcode/config.local.yaml`

合并规则：

- `hooks` 不是覆盖，而是 `extend` 追加
- 所以后面的配置层可以继续追加新的 hook

## 2. 顶层 schema

最外层写法是一个 list：

```yaml
hooks:
  - id: block-rm
    event: pre_tool_use
    if: 'tool == "Bash" && args.command =~ /rm\s+-rf/'
    reject: true
    action:
      type: command
      command: echo dangerous command blocked
```

注意：

- `validator.py` 只校验 `hooks` 是不是 list
- 真正的字段校验在 `mewcode/mewcode/hooks/loader.py`

## 3. Hook 字段

每条 hook 目前支持这些字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 否 | 不填时自动生成为 `<event>_<index>` |
| `event` | 是 | 生命周期事件名 |
| `if` | 否 | 条件表达式 |
| `action` | 是 | 执行动作 |
| `reject` | 否 | 仅 `pre_tool_use` 可用，拒绝工具执行 |
| `once` | 否 | 只执行一次 |
| `async` | 否 | 异步后台执行，`pre_tool_use` 禁用 |

## 4. 事件枚举

`mewcode/mewcode/hooks/events.py` 一共声明了 15 个事件：

### 4.1 已真实接线的事件

这些事件在 `agent.py` 或 `app.py` 里确实有调用点：

| 事件 | 触发位置 |
|---|---|
| `startup` | app 启动时 |
| `shutdown` | app 退出清理时 |
| `session_start` | agent run 开始时 |
| `session_end` | agent 正常结束时 |
| `turn_start` | 每轮开始 |
| `turn_end` | 每轮结束 |
| `pre_send` | 请求发给模型前 |
| `post_receive` | 收到模型文本后 |
| `pre_tool_use` | 工具执行前 |
| `post_tool_use` | 工具执行后 |

### 4.2 仅声明、未发现触发点的事件

这些值在枚举里声明了，但在 `agent.py` / `app.py` 里没有实际 `run_hooks(...)` 调用：

| 事件 |
|---|
| `error` |
| `compact` |
| `permission_request` |
| `file_change` |
| `command_execute` |

这部分不能当成“现成能力”看，只能算保留接口。

## 5. 条件表达式 DSL

条件解析在 `mewcode/mewcode/hooks/conditions.py`。

支持运算符：

| 运算符 | 含义 |
|---|---|
| `==` | 字符串相等 |
| `!=` | 字符串不等 |
| `=~` | 正则匹配 |
| `~=` | glob 匹配 |

支持逻辑连接：

- `&&`
- `||`

限制：

- 一条表达式里不能混用 `&&` 和 `||`
- 混用会直接抛配置错误

### 5.1 可用于条件判断的字段

`HookContext.get_field()` 实际只支持：

| 字段 | 说明 |
|---|---|
| `tool` | 当前工具名 |
| `event` | 当前事件名 |
| `args.xxx` | 工具参数字段 |

这意味着：

- 可以判断 `tool == "Bash"`
- 可以判断 `args.command =~ /rm/`
- 不能直接在 `if` 里判断 `file_path`、`message`、`error`

这一点比 `NZ-Coder` 现在要弱很多。

## 6. Action 类型

`mew` 当前支持 4 种 action type。

### 6.1 `command`

必填字段：

- `command`

可选字段：

- `timeout`，默认 `30`

行为：

- 用 `asyncio.create_subprocess_shell()` 执行 shell
- 合并 stdout/stderr
- 超时后 kill 进程

### 6.2 `prompt`

必填字段：

- `message`

行为：

- 返回一段字符串
- HookEngine 会把它加入 `hook_prompts`
- 最终被拼进 system prompt 末尾的 `# Hook Injected Context`

这是 `mew` hook 最稳定的一类能力。

### 6.3 `http`

必填字段：

- `url`

可选字段：

- `method`，默认 `POST`
- `body`
- `headers`

行为：

- 通过 `urllib.request.urlopen` 发请求
- 若提供 `body` 且没写 `Content-Type`，默认补 `application/json`

### 6.4 `agent`

必填字段：

- `prompt`

行为：

- 目前只是 stub
- 返回 `"agent executor not yet implemented"`

所以这个类型现在并不能真正起到子 agent 或 review agent 作用。

## 7. 模板变量替换

`mewcode/mewcode/hooks/models.py` 的 `HookContext.expand()` 支持这些模板变量：

| 变量 | 说明 |
|---|---|
| `$EVENT` | 事件名 |
| `$TOOL_NAME` | 工具名 |
| `$FILE_PATH` | 文件路径 |
| `$MESSAGE` | 消息文本 |
| `$ERROR` | 错误文本 |
| `$TOOL_ARGS.xxx` | 工具参数 |

注意两点：

1. 这些变量可用于 `command` / `prompt` / `http` / `agent` 的字符串模板
2. 变量能展开，不代表它也能用于 `if` 条件判断

## 8. 控制位语义

### 8.1 `reject: true`

限制：

- 只能用于 `pre_tool_use`

行为：

- hook action 会先执行
- action 的输出文本会成为拒绝原因
- 工具结果会变成 `Hook rejected: <reason>`

### 8.2 `once: true`

行为：

- 命中过一次后把 `executed=True`
- 后续同一个 hook 不再执行

### 8.3 `async: true`

限制：

- `pre_tool_use` 禁止使用

行为：

- 用 `asyncio.ensure_future()` 后台执行
- 主流程不等待其完成

这意味着异步 hook 更像旁路通知，不适合做强约束。

## 9. Loader 校验规则

`mewcode/mewcode/hooks/loader.py` 当前做的核心校验：

1. `event` 必须在枚举里
2. `action.type` 必须是 `command/prompt/http/agent`
3. action 必填字段必须齐全
4. `reject` 只能配 `pre_tool_use`
5. `async` 不能配 `pre_tool_use`
6. `timeout` 必须是正整数
7. `if` 条件表达式解析失败会报错

## 10. 运行时行为

`HookEngine` 的核心逻辑：

1. 找出 event 匹配且条件成立的 hook
2. `once` hook 执行后打标记
3. `prompt` action 的输出单独缓存
4. 普通 hook 通过 `run_hooks()` 执行
5. `pre_tool_use` 通过 `run_pre_tool_hooks()` 执行，并支持 `reject`

补充说明：

- `prompt` 消息会被注入到 system prompt
- hook 的执行结果还会形成 `HookEvent`，给 UI 或上层消费
- `agent.py` 里对 hook 的 notification 会及时 drain，所以它主要是事件流，不是长期状态

## 11. 当前 `mew` 值得直接抄的点

这些部分可以直接借鉴：

1. `hooks` 作为主配置的一部分，而不是再单独发明一套文件
2. `event + if + action + reject/once/async` 的整体结构
3. 简单条件 DSL：`== / != / =~ / ~=`
4. `pre_tool_use` 专门负责阻断
5. `prompt` 作为 system prompt 注入手段
6. 多层配置文件 merge 时对 hooks 使用追加语义

## 12. 不建议原样照抄的点

这些点 `mew` 做得并不强：

1. `validator.py` 对 hook 几乎不做深校验，真正校验散在 loader
2. 声明了 15 个事件，但真正接线只有 10 个
3. `agent` action 只是 stub，没有实用价值
4. 条件上下文字段很少，只能看 `tool/event/args.*`
5. `async` hook 是 fire-and-forget，没有更强的回收、失败传播和顺序保证

## 13. 和当前 NZ-Coder 的差异

对比当前 `NZ-Coder`：

### 13.1 我们现在比 `mew` 强的地方

1. 条件上下文更丰富
2. 已支持 `continue: true` 的 `no_tool_response` reopen 机制
3. 已支持 `on_error` / `error_message`
4. 已支持基于任务验收的字段：
   `requested_paths`、`missing_requested_test_paths_count`、`same_basename_conflict`、`wants_tests`、`tests_modified`
5. reflection 已经挂进默认 hook 流程，而不是只靠模型自觉结束

### 13.2 我们还没抄到的地方

1. action type 目前基本只有 `prompt`
2. 没有 `command/http` 这类 side-effect action
3. 没有多层 config merge
4. 没有 `startup/shutdown/session_start/session_end` 这类更完整的事件面

## 14. 建议的抄法

如果要“抄 `mew` 的 hook 配置文件”，建议只抄下面这些：

1. 结构层：
   `hooks[] -> {id,event,if,action,reject,once}`
2. 语义层：
   `pre_tool_use` 负责拦截，`post_tool_use/turn_end` 负责提示
3. 配置层：
   可以考虑后续支持项目级和用户级两层 merge

不建议直接抄的部分：

1. 15 个事件全量枚举
2. `agent` action
3. 过弱的上下文字段设计

## 15. 结论

`mew` 的 hook 配置文件本质上是一个“通用生命周期规则表”，优点是结构清晰，缺点是很多能力停留在接口层，没有完全跑通。

对 `NZ-Coder` 来说，最值得借鉴的不是它“声明了多少 hook”，而是：

1. 用统一配置表达规则
2. 把阻断和提示分离
3. 让 hook 成为 system prompt 注入入口

我们当前的 hook runtime 在“任务约束”和“验收上下文”上已经比它更强，后续如果要继续补，优先级应该是：

1. 扩充 event 面
2. 增加可选 action type
3. 做用户级/项目级 hook merge
