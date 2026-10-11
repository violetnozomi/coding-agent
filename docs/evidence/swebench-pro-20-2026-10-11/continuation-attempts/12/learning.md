# 第 12 题复盘：GUI 子进程 stderr 实时呈现

冻结补丁连接 readyReadStandardError，在信号到达时读取 stderr、按项目编码解码，更新累积输出并呈现错误消息。已有 stdout 的回车覆盖规则被提取为 _apply_cr，与 stderr 复用；两条输出流分别维护，避免互相覆盖。

Qt 信号触发、进程结束时尾部数据、重复读取与累积文本需要分别检查。Agent 还修改了项目测试并保留三个探测文件；独立官方验收使用原始环境及官方测试，必需检查 42/42 通过。Agent 在 40 次主请求后 max_turns，不能把官方通过反写为正常完成。

面试讲解可以围绕：事件驱动读取与退出后集中读取的差别；回车与换行语义；消息替换标识与输出流隔离；功能验收通过和 Agent 正常结束为什么是不同事实。没有运行 Windows，本题不证明 Windows GUI 行为。

证据：[补丁](model.patch)、[实际命令](command-evidence.json)、[运行终态](report.json)、[官方结果](evaluation-result.json)。
