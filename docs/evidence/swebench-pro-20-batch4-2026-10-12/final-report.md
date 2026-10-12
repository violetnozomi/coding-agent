# 第四批固定20题：真实并行评估与闭账

排除前三批80个题号，新增20个不同题目。实际调用主模型20题，官方独立验收16通过、3未通过、1未知或未运行。Agent终态与功能验收分开记录。

| 题号 / 轨迹 | 主 / 辅助请求 | Agent终态 | 官方独立验收 |
|---|---:|---|---|
| [01](attempts/b4-01/README.md) | 40 / 0 | max_turns | 通过 |
| [02](preparation-retry-attempts/b4-02/README.md) | 21 / 0 | completed | 通过 |
| [03](attempts/b4-03/README.md) | 40 / 0 | max_turns | 通过 |
| [04](attempts/b4-04/README.md) | 40 / 0 | completed | 通过 |
| [05](attempts/b4-05/README.md) | 40 / 0 | completed | 通过 |
| [06](preparation-retry-attempts/b4-06/README.md) | 40 / 0 | max_turns | 通过 |
| [07](attempts/b4-07/README.md) | 40 / 0 | max_turns | 通过 |
| [08](attempts/b4-08/README.md) | 40 / 0 | completed | 通过 |
| [09](final-unattempted/b4-09/README.md) | 40 / 0 | max_turns | 未通过 |
| [10](attempts/b4-10/README.md) | 40 / 0 | max_turns | 通过 |
| [11](attempts/b4-11/README.md) | 40 / 0 | max_turns | 通过 |
| [12](preparation-retry-attempts/b4-12/README.md) | 40 / 0 | max_turns | 通过 |
| [13](attempts/b4-13/README.md) | 40 / 0 | max_turns | 未知 |
| [14](attempts/b4-14/README.md) | 40 / 0 | max_turns | 通过 |
| [15](memory-attempts/b4-15/README.md) | 40 / 0 | max_turns | 未通过 |
| [16](attempts/b4-16/README.md) | 40 / 0 | max_turns | 未通过 |
| [17](attempts/b4-17/README.md) | 40 / 0 | max_turns | 通过 |
| [18](attempts/b4-18/README.md) | 40 / 0 | completed | 通过 |
| [19](attempts/b4-19/README.md) | 30 / 0 | completed | 通过 |
| [20](attempts/b4-20/README.md) | 40 / 0 | max_turns | 通过 |

实际主/辅助请求771/0。本批总授权人民币50元，估算占用41.63319080元，未知或其他保留0.00000000元，未用8.36680920元在本批结束后关闭，不转为后续授权。供应商实际账单未知。

复用现有原生Agent、4路有界并行执行器和共享费用账本；镜像准备串行，大环境依空间准入处理。付费失败题不重跑；前序请求完全结算后，只首次运行零模型调用题。所有阶段Core和原生入口冻结一致，模型、40主请求/6辅助请求及3000秒口径不变。

实际出站max_tokens/max_completion_tokens、thinking、reasoning_effort及stream见[逐笔核验](final-accounting-and-integrity.json)。393216为费用预留，非输出限制；未设置不写成medium/false。估算不是供应商账单，缺失usage或缺失字段保留unknown/null和观测数。

容器独立工作区、HOME及Session，无网络、非root；密钥仅在宿主Relay。Agent不接触官方隐藏验收，停止后冻结实际补丁，在新鲜原始镜像执行未改的官方验收。公开协议和工具记录脱敏，原始Session/Artifact及Provider原件保留本机并提供SHA256。宿主没有提供正确补丁或向模型泄露验收结果。

第9/15题仅按[单独存储授权](memory-workspace-authorization.json)使用内存源码工作区，归档成功后清理。第9题原镜像空间估计拒绝保留；在已授权内存目录核验官方压缩层及解压diff_ids并量取尺寸，独立准备峰值含压缩临时层、全部tar、逐entry分配余量及原512MiB止损。镜像准备完成后再走原2GiB任务准入；官方manifest/config/全部diff_ids保持一致，不改变Agent或验收器。[环境干预记录](memory-environment-intervention.json)不计为编码进展。

第13题官方验收停在补丁应用阶段：三个Redis appendonlydir文件already exists，退出97，没有启动官方测试。初态hash只覆盖tracked文件，不能证明冲突文件与初态相同；原补丁保持不变。该项记unknown，不将验收阻断当作测试断言失败或通过。细节见[原始阻断事实](attempts/b4-13/acceptance-blocker.json)。

本批Agent终态6个completed、14个max_turns，不能用正常结束替代功能验收。12次辅助尝试在本地以input_token_bound_unknown拒绝，实际辅助出站为0；原始本地拒绝仍保留。

本轮未修改Core、验证器或评分函数。导出阶段排序反例修复：python -m pytest -q tests/evaluation/test_batch4_evidence_chronology.py，2 passed；仍禁止覆盖已付费尝试。未运行全仓或Windows，不重跑未变的检查。历史compaction fake失败test_context_overflow_stops_after_three_compaction_attempts、test_pre_send_and_reactive_compactions_share_one_three_attempt_owner保留，不删除或弱化。

每题README与learning.md提供请求→工具事实→补丁→独立验收的面试复盘入口。冻结验收通过只证明其覆盖范围，不追认Agent未运行的测试。本批结果不能证明整体SWE成功率、Core效果提高或并行提速倍数。
