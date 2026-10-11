# 第 10 题复盘：字典文本的安全解析

冻结补丁在 JSON 解析失败后使用 literal_eval，仅接受字典；集合等其他合法 Python 字面量仍拒绝。key=value 路径的无效字段转成 TypeError。literal_eval 的受限字面量语义与执行任意表达式不同，不能把“解析成功”当成“类型符合要求”。

实际项目 Python 为 3.11.13，本题没有使用隔离 Agent Python 3.13。37 次真实主请求，Agent completed；独立官方必需检查 2/2 通过。公开 Agent 测试与最终独立验收应分开引用。

面试讲解可以说明：JSON 与 Python 字面量的输入差异；为何需要解析后类型校验；如何保持失败异常契约；如何补充恶意表达式、集合和坏键值字段的反例。

证据：[补丁](model.patch)、[环境](environment.json)、[真实命令](command-evidence.json)、[官方结果](evaluation-result.json)。
