# 第 18 题：新增电子书提供方需要覆盖检索和阅读入口

OpenLibrary 实例 `e010b2a13697de70170033902ba2e27a1e1acbe9`。一次真实 Agent 尝试，40 个主请求、0 个辅助请求，终态 `max_turns`；官方独立验收 11 项通过。已返回 usage 的保守峰时估算 1.89001144 元，不是账单。

## 实际修改

[冻结补丁](model.patch) 新增 Project Runeberg provider、标识与开放阅读 URL，并注册到已有 provider 顺序；将检索字段贯通 worksearch schema 和返回文档；新增阅读按钮、提示模板和 provider 测试，同时更新既有文档返回测试的预期字段。

Agent 使用 grep/read 查找已有提供方的实现与模板，再提交跨文件改动。没有主机提供补丁、透露官方验收或重跑 Agent。

## 验证范围

真实运行时对 `openlibrary/book_providers.py` 的五次 `py_compile` 成功，只证明该文件编译检查通过。模型提出的 pytest 命令返回未执行的分发错误；新写测试没有在 Agent 轨迹中真正运行。不能把新增测试正文或多次静态检查当作功能复测通过。[命令事实](command-evidence.json) 保留每次执行状态。

冻结后，在新的原始镜像上运行未修改的官方 verifier，11 项通过，[官方结果](evaluation-result.json) 为 `resolved=true`。与 Agent 的轮次耗尽、未运行 pytest 分开报告，不能反向补全历史验证事实。

## 面试复盘

- 解释 provider 注册、标识传播、检索返回和阅读模板之间的依赖，避免只添加一个孤立类。
- 通过原有提供方比较 URL、标识选择、开放访问和返回数据结构。
- 给出实际运行命令，并区分编译检查、未执行测试和独立功能验收；未知范围保留未知。

请求、响应、工具、权限、初终态文件和原始 Session/Artifact 均已保存。
