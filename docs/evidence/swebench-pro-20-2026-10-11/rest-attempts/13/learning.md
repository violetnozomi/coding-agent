# 第 13 题：监控调度器从阻塞运行迁移到 asyncio

Open Library 实例 `8a5a63af...-v0f5aece...`。一次真实 Agent 尝试，39 个主请求、0 个辅助请求，约 470 秒，正常 `completed`。官方独立验收通过；已返回 usage 的保守峰时估算 1.89323872 元，不是账单。

## 实际修改与恢复

Agent 修改 `scripts/monitoring/utils.py`、`monitor.py` 和已有测试：提取共享调度器行为，保留 `OlBlockingScheduler`，新增基于 `AsyncIOScheduler` 的 `OlAsyncIOScheduler`；监控入口通过 `asyncio.run(main())` 启动并等待异步事件。服务器过滤逻辑适配两个调度器，保持默认任务 ID 与函数名一致。

其中一次 `edit_file` 未匹配到旧文本，随后真实 `read_file` 重读同一文件，再继续局部修改。这是本次工具链实际发生的恢复，不是受控 Provider 预填补丁。完整参数、结果和后续请求见 [运行时](runtime.jsonl) 与 [模型请求](provider-requests.jsonl)。

## 验证事实与范围

真实运行了 `python -m pytest scripts/monitoring/tests/test_utils_py.py -q`，当前版本 4 项通过；日志里的 pytest-asyncio 配置弃用警告不是断言失败。还实际导入监控模块并打印注册任务，获得 `jobs: ['log_workers_cur_fn']`。另外几种设置 HOSTNAME 或 Shell 循环的命令被拒绝，不能伪造它们已执行。

停止后冻结的 [补丁](model.patch) 在新原始镜像中通过官方独立验收，[结果](evaluation-result.json) 为 `resolved=true`。本次公开验证主要覆盖调度器类型、任务注册与服务器过滤；没有记录实际长期运行监控服务或发送中断信号的测试，因此不声称所有关闭和取消边界都已验证。

## 面试复盘

- 解释阻塞调度器与事件循环调度器的入口区别，以及为什么共用任务 ID、监听器和过滤行为。
- 讲清编辑失败后重读当前文本，再构造精确替换，避免未经确认整文件覆盖。
- 区分测试通过、弃用警告、权限拒绝与未验证的生命周期边界。

本题完整 Session/Artifact 正常归档，回执保留原件位置与 SHA-256。没有人工改题目文件、补填正确补丁或重跑付费尝试。
