# .nz-coder/settings.json 配置说明

将此文件复制到你项目的 `.nz-coder/settings.json` 以配置权限规则。

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
  }
}
```

## 规则语法

| 规则 | 含义 |
|---|---|
| `"bash"` | 匹配所有 bash 命令 |
| `"bash(prefix:git )"` | 只匹配以 `git ` 开头的 bash 命令 |
| `"write_file"` | 匹配所有 write_file 调用 |

## 权限模式说明

| 模式 | 行为 |
|---|---|
| `default` | 读操作自动允许，写操作和 bash 需要确认 |
| `auto` | 全部自动允许（危险命令除外） |
| `plan` | 只允许读操作，禁止所有写和 bash |
| `acceptEdits` | 允许文件编辑，bash 和其他写操作仍需确认 |
