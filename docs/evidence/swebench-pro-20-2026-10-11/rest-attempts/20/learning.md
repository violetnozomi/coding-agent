# 第 20 题：区分非 ISBN 的 ASIN，并保留来源优先级

OpenLibrary 实例 `d8162c226a9d576f094dc1830c4c1ffd0be2dd17`。一次真实 Agent 尝试，15 个主请求、0 个辅助请求，约 42 秒，Agent 正常 completed，官方独立验收通过。已返回 usage 的保守峰时估算为 0.26343648 元，见 [task-outcome.json](task-outcome.json)；没有取得供应商账单，估算不是实际费用。

## 修改依据

[冻结补丁](model.patch) 新增 `get_non_isbn_asin`：先检查 amazon identifiers 中以 B 开头的值，再检查 source_records 中的 amazon:B 项；不存在则返回 None。`is_asin_only` 在存在 ISBN-10 或 ISBN-13 时返回 False，否则依据前述查找。新增参数化测试覆盖空记录、ISBN 形态、多个值、来源优先级和 ISBN 与 ASIN 并存等情况。

## 实际复测

最初带 `cd /workspace &&` 的 pytest 调用未执行。Agent 自行改用 Bash 的 workdir 参数，真正运行 `python -m pytest openlibrary/tests/catalog/test_utils.py -q`，输出 88 passed；之后再次执行相同目标并做一个 source_records 调用探测，仍通过。虽然命令经 tail 展示，测试通过结论来自明确的 pytest 汇总，不仅是 Shell 最后退出码。

更大范围的 catalog 测试调用返回工具错误，不能声称目录或全项目测试通过。[命令事实](command-evidence.json) 保留该项，未用独立验收反向覆盖。

冻结后，新原始镜像上的未修改官方 verifier 报告 79 个通过条目，[结果](evaluation-result.json) 为 `resolved=true`。没有主机替模型写补丁、提供答案或付费重跑。此次真实模型完成了读取、局部实现、补测试和目标复测；没有编造修改前运行失败的历史。

## 面试复盘

- 明确字段来源优先级和返回 None 的语义，避免将 ISBN 形态的 Amazon 标识当作 ASIN。
- 用参数化测试表达空值、多个来源和相互排斥的条件。
- 解释工作目录参数与复合 Shell 的区别，分别报告目标测试、范围扩大失败和独立验收。

请求、响应、工具、权限、文件版本及完整 Session/Artifact 均已保存。
