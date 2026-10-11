# 第 17 题：上传前验证目标目录，避免隐式创建

NodeBB 实例 `f9ce92df988db7c1ae55d9ef96d247d27478bc70`。一次真实 Agent 尝试，40 个主请求、0 个辅助请求，终态 `max_turns`；官方独立验收通过。已返回 usage 的保守峰时估算 1.7848036 元，不是账单。

## 修改依据

[实际补丁](model.patch) 在管理员普通文件上传调用 `saveFileToLocal` 之前，检查结合上传根目录和请求 folder 得到的目录是否在允许范围且已经存在；不满足时使用已有 invalid-path 错误。新增测试要求不存在的目录不能因上传而被创建。

这次改动只涉及 controller 与上传测试。官方通过只证明其测试覆盖，不证明字符串前缀检查已经穷尽符号链接、同名前缀等所有路径安全情形；本轮没有扩大修改范围。

## 实际运行了什么

Agent 对 controller 和测试文件分别成功执行 `node --check`，只说明语法有效。多次 Mocha、配置和目录检查请求返回未执行的分发错误；不能把它们当作真实测试失败或通过。工具正文只有笼统错误，原始参数和事实保留在 [command-evidence.json](command-evidence.json)。

终止后，全新原始镜像上的未修改官方 verifier 对冻结补丁给出 `resolved=true`。[官方日志](evaluation/run-script-stdout.txt) 与 [结果](evaluation-result.json) 保留。官方复测通过不追认 Agent 自己完成了功能测试，也不把 `max_turns` 改成 completed。没有人工替 Agent 修改文件或付费重跑。

## 面试复盘

- 解释验证应发生在写文件之前，以及拒绝不存在目录与自动创建目录的行为差异。
- 区分路径字符串校验、真实目录状态和完整安全边界。
- 用实际命令说明语法检查、功能测试、独立验收各自证明的范围，并指出未执行命令给后续决策造成的信息缺口。

模型请求、响应、文件前后版本、原始 Session/Artifact、工具、权限与验收均已归档。
