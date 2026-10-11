# SWE-bench Pro V2：第二批固定 20 题

20 个新实例均已完成一次真实 Agent 尝试，与第一批无重合。官方独立验收 **17 题通过、3 题未通过**；Agent 自身终态为 10 completed、10 max_turns，两者分开记录。

真实主/辅助模型请求 **750/0**。本批授权人民币 50 元，usage 按峰时价格估算 **37.96093664 元**，无缺失 usage 或未结算请求；供应商实际账单未知。上一批余额未沿用，本批剩余额度不自动授权下一批。

- [逐题结果和范围](final-report.md)
- [20 题规范结果索引](current-outcomes.json)
- [逐阶段费用、请求、补丁及归档身份校验](final-accounting-and-integrity.json)
- [实际并行窗口](actual-parallel-windows.json)
- [固定任务清单](task-manifest.json)
- [环境下载恢复记录](environment-recovery/README.md)
- [阶段历史与准备阻断](stage-notes-before-final.md)

每题从结果表进入 README 和 learning.md，按 provider-requests.jsonl → provider-responses.jsonl → runtime.jsonl/permissions.jsonl → model.patch → evaluation 复盘。公开轨迹去除凭据和私有推理正文；raw-identity.json 提供原始请求/响应哈希，archive-receipt.json 提供完整私有 Session/Artifact 归档位置及哈希。

三道未通过题：04 的官方 Go 编译缺少新增依赖的 go.sum 记录；06 的新增测试导入未声明模块，测试未进入断言；08 的 Agent 连续调查后未产生补丁，官方测试出现真实断言失败。对应补丁、执行/未执行事实及错误全文均保留，不重跑到通过。

两个隔离任务并行，共享原子计费账本；镜像准备串行。大镜像按磁盘容量逐题处理，原准备失败且零模型请求的题在本批剩余额度内首次运行。各阶段冻结 Agent 代码文件哈希相同。显式 SWE 工具/权限集合、每题 40 主请求、6 辅助请求和 3000 秒边界保持原口径；实际请求未设置输出 token 上限、thinking、reasoning_effort 或 stream，不把未设置写成 false/medium。

首次执行命令：

```sh
python docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py prepare
python docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py execute
```

准备失败续接使用同一脚本的 resume-preparation；只允许已闭账后的零模型调用题，不是付费失败重试。run-parallel.py 的阶段目录使用独占创建，防止无意重复执行。本批已结束，不要用旧授权重新运行。

相关离线回归 ModelRelay 58 passed、续接与套接字路径 6 passed。未运行全仓或 Windows；历史 compaction fake 两项失败按原证据保留。这里只报告固定小样本结果，不宣称总体 SWE 成功率、InfCodeX 对齐比例或并行提速倍数。

私有原件：`/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch2-2026-10-11`。
