# SWE-bench Pro V2 第二批 20 题

本批授权：人民币 50 元，`deepseek-flash`，包含主模型及全部辅助模型。上一批已关闭，余额不沿用。

名单采用上一批同一公开 ID 哈希顺序的后续 20 题，排除上一批全部实例；不按成绩换题。官方源码固定 `scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862`。

两个隔离任务并行，宿主共用一份原子计费账本。镜像构建串行，读取、修改和项目测试由真实生产 NativeSDKRunner 执行。每题最多 40 个主请求、6 个辅助请求，实际 Agent 时限 3000 秒，保持上一批口径。主请求不传输出 token 限制；393216 token 仅用于费用预留，不注入 Provider 请求。Provider usage 缺失时保留预留，不记零费用。

每题冻结补丁后，在原始项目镜像执行官方 verifier；隐藏验收不挂入 Agent。保留原始请求/响应、Runtime、权限、Session/Artifact 归档、初态/终态、补丁及验收日志。公开轨迹移除凭据和 reasoning_content，原件保留在本机私有记录中。

私有原件：`/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch2-2026-10-11`。

命令：

```sh
python docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py prepare
python docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py execute
```

准备和测试结果由本批实际执行记录补充；运行结束、Agent 完成、实际测试通过和官方验收分别报告，不合并为同一结论。

首批检查点：第 1、2 题均已完成官方验收并通过；Agent 状态分别为 `max_turns`、`completed`。原件与学习入口见 [第 1 题](attempts/b2-01/README.md)、[第 2 题](attempts/b2-02/README.md)。其余题仍执行中，本检查点不代表整批完成。
