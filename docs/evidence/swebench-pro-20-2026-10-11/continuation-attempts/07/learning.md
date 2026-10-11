# 第 07 题复盘：Windows KB 数据与预期同步

冻结补丁在 scanner/windows.go 的三个发行分支补充 revision→KB 项，同时更新 scanner/windows_test.go 的对应列表。这是已有匹配算法的数据维护，不是增加新的扫描算法。

实际工具记录包含不合法的超时参数及被拒绝的 Shell 前缀，随后改用允许的 Go 测试命令。复盘时应分别检查未执行调用与真正测试结果，不能把前两者算成测试失败。最终 Agent completed；独立官方必需检查 7/7 通过。

面试讲解可以围绕：数据驱动逻辑为什么需要同时维护预期列表；如何保证只变更目标发行分支；如何区分工具参数错误、权限拒绝与断言失败。这里证明的是冻结题目要求的检查，不代表全部 Windows 发行数据正确。

证据：[补丁](model.patch)、[命令事实](command-evidence.json)、[官方结果](evaluation-result.json)、[请求](provider-requests.jsonl)。
