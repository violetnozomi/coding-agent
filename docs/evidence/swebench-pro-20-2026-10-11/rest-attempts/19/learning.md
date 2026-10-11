# 第 19 题：把自定义 home 贯通启动配置、profile 和密钥目录

Teleport 实例 `326fd1d7be87b03998dbc53bc706fdef90f5065c`。一次真实 Agent 尝试，40 个主请求、0 个辅助请求，约 1215 秒，终态 `max_turns`；官方独立验收通过。已返回 usage 的保守峰时估算 5.60014856 元，不是账单，没有付费重跑。

## 修改链路

[实际补丁](model.patch) 给 CLI 与客户端配置增加 HomePath；通过可注入的环境查询函数读取 `TELEPORT_HOME`，非空时用 `path.Clean` 归一化，并在启动构建客户端前调用。客户端将值传入 profile 路径，KeysDir 未显式设置时跟随该 home；profile 读写没有显式目录参数时使用配置值。另补充环境变量与路径清理测试。

Agent 先追踪启动、客户端配置、profile 和测试，再提交修改。没有宿主机注入补丁或纠偏提示。实际验收在 Linux 运行，不能据此宣称 Windows 已覆盖。

## 失败检查没有被洗成通过

运行时真正执行 `go test ./lib/client -run '^$'`，结果非零；它只走编译检查，不执行功能测试。随后 `verify_changed_files` 的工具状态虽然是 ok，正文明确为 FAIL，并给出 Go 编译器创建 OS 线程失败、`errno=11`、`newosproc` 等环境诊断。工具返回成功与内部验证通过必须分开判断。

模型随后提出降低并发的构建、vet 和 gofmt 命令，但这些调用返回未执行的分发错误。新增测试没有在 Agent 阶段实际跑出通过结果。[命令事实](command-evidence.json) 与 [runtime](runtime.jsonl) 保留执行、未执行和失败证据，不将环境失败误报成测试断言失败。

冻结后，全新原始镜像运行未修改官方 verifier，通过 `TestReadTeleportHome`、`TestMakeClientHomePath` 及其报告的相关回归，[独立结果](evaluation-result.json) 为 `resolved=true`。这不抹除 Agent 内的编译环境失败，也不把 `max_turns` 反写成 completed。

## 面试复盘

- 配置值必须在客户端构建前解析，并贯通所有实际读写入口；只在 CLI 增加字段不足以完成需求。
- 区分用户显式设置、空值默认和密钥目录的独立覆盖，避免无条件覆盖显式配置。
- 使用注入的环境查询函数写确定性测试，并解释 OS 线程不足与代码编译错误的区别。

完整请求/响应、初终态、冻结 diff、Session/Artifact、权限与官方日志已经保留。
