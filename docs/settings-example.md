# .nz-coder/settings.json 配置说明

将此文件复制到你项目的 `.nz-coder/settings.json` 以配置权限规则和 hook 规则。

```json
{
  "permissions": {
    "allow": [
      "bash(prefix:git )",
      "bash(prefix:python3 -m pytest)",
      "bash(prefix:make )"
    ],
    "deny": [
      "bash(prefix:rm -rf)",
      "bash(prefix:sudo )",
      "bash(prefix:curl )",
      "bash(prefix:wget )"
    ],
    "ask": [
      "bash(prefix:pip )",
      "bash(prefix:npm )"
    ]
  },
  "hooks": [
    {
      "id": "block-same-basename-wrong-dir",
      "event": "pre_tool_use",
      "if": "same_basename_conflict == \"true\"",
      "action": {
        "type": "prompt",
        "message": "User requested $CONFLICTING_REQUESTED_PATH, but you are trying to write $FILE_PATH. Edit the exact requested path instead of creating a same-basename file elsewhere."
      },
      "reject": true
    },
    {
      "id": "reopen-missing-tests",
      "event": "no_tool_response",
      "if": "missing_requested_test_paths_count != \"0\"",
      "action": {
        "type": "prompt",
        "message": "Missing requested test files: $MISSING_REQUESTED_TEST_PATHS. Add them before finishing."
      },
      "continue": true,
      "once": true
    },
    {
      "id": "reopen-when-task-wants-tests",
      "event": "no_tool_response",
      "if": "wants_tests == \"true\" && tests_modified == \"false\"",
      "action": {
        "type": "prompt",
        "message": "The task explicitly asks for tests, but no test-file change was recorded. Add or update the relevant tests before finishing."
      },
      "continue": true,
      "once": true
    }
  ]
}
```

## 权限规则语法

| 规则 | 含义 |
|---|---|
| `"bash"` | 匹配所有 bash 命令 |
| `"bash(prefix:git )"` | 只匹配以 `git ` 开头的 bash 命令 |
| `"write_file"` | 匹配所有 `write_file` 调用 |

## Hook 规则语法

### 可用事件

| 事件 | 说明 |
|---|---|
| `turn_start` | 每轮开始时触发 |
| `pre_send` | 请求发送给模型前触发 |
| `post_receive` | 收到模型回复后触发 |
| `pre_tool_use` | 工具真正执行前触发，可 `reject: true` |
| `post_tool_use` | 工具执行后触发 |
| `turn_end` | 一轮结束时触发 |
| `no_tool_response` | 模型未调用工具、准备结束时触发，可 `continue: true` |

### 条件运算符

| 运算符 | 含义 |
|---|---|
| `==` | 字符串相等 |
| `!=` | 字符串不等 |
| `=~` | 正则匹配 |
| `~=` | glob 匹配 |

### 常用 hook 字段

| 字段 | 含义 |
|---|---|
| `tool` | 当前工具名 |
| `args.path` | 工具参数里的 `path` |
| `file_path` | 当前工具的主路径 |
| `file_basename` | 当前路径 basename |
| `same_basename_conflict` | 是否命中了“同 basename 但不是用户要求的精确路径” |
| `conflicting_requested_path` | 与当前路径冲突的用户目标路径 |
| `requested_path_exact_match` | 当前路径是否就是用户明确要求的路径 |
| `requested_basename_match` | 当前路径 basename 是否与某个用户目标路径同名 |
| `wants_tests` | 任务是否明确要求测试 |
| `tests_modified` | 本轮运行是否记录到了测试文件改动 |
| `missing_requested_paths_count` | 仍未产出的用户目标路径数量 |
| `missing_requested_test_paths_count` | 仍未产出的用户目标测试路径数量 |
| `missing_requested_test_paths` | 仍未产出的用户目标测试路径列表 |

### 行为字段

| 字段 | 含义 |
|---|---|
| `reject: true` | 仅 `pre_tool_use` 可用。阻止本次工具执行 |
| `continue: true` | 仅 `no_tool_response` 可用。阻止本轮结束，继续让 agent 工作 |
| `once: true` | 规则只触发一次，避免重复循环 |
| `on_error` | hook 自身出错时的策略：`ignore` / `log` / `prompt` / `reject` |
| `error_message` | hook 出错时注入的自定义提示 |

## 权限模式说明

| 模式 | 行为 |
|---|---|
| `default` | 读操作自动允许，写操作和 bash 需要确认 |
| `auto` | 全部自动允许（危险命令除外） |
| `plan` | 只允许读操作，禁止所有写和 bash |
| `acceptEdits` | 允许文件编辑，bash 和其他写操作仍需确认 |
