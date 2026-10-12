# 第 15 题复盘练习

实例 `instance_protonmail__webclients-c5a2089ca2bfe9aa1d85a664b8ad87ef843a1c9c`。真实主/辅助请求 40/0，Agent 终态 `max_turns`，官方独立结果 `False`。

按实际记录阅读：[请求](provider-requests.jsonl) → [响应](provider-responses.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [独立验收](task-outcome.json)。原始请求/响应的哈希配对见 [原件身份](raw-identity.json)。

没有记录到 executed=true 且 command_failed=true 的 Bash 结果；不能据此推断所有必需测试已运行。核对拒绝、工具失败和独立验收。
没有记录到成功执行的写工具；核对补丁和是否需要修改，不能用 completed 推断有有效修复。

面试练习：

1. 在原始请求中找出模型修改前实际获得的定位证据；哪些信息不足，下一步该读哪里？
2. 对照 patch 和真实测试命令，解释修改假设、验证覆盖及未证明的边界。
3. 比较 Agent 终态与冻结补丁的独立验收；指出权限拒绝、环境失败、测试失败与功能未完成的区别。

公开文件不含私有推理正文。这里的提问用于复盘，不替模型补造推理或测试事实。
