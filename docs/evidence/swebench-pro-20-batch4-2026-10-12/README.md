# SWE-bench Pro V2：第四批20题与100题总记录

新增20个不同题目均已实际调用模型。官方独立验收16题通过、3题未通过、1题未知：第13题在补丁应用时阻断，未启动官方测试，保留原补丁和错误，不补造成绩。Agent终态为6个completed、14个max_turns，与功能验收分别记录。

四批共100个不同题目，100题实际运行：80题官方通过、19题未通过、1题验收阻断。各批资源与环境并非统一配对实验，不据此宣称整体SWE成功率或能力提升。

- [本批逐题最终报告](final-report.md)
- [100题总结果与各批记录位置](cumulative-100-outcomes.json)
- [本批20题可追溯终态](current-outcomes.json)
- [请求、usage和完整归档核验](final-accounting-and-integrity.json)
- [人民币50元授权闭账](authorization-closed.json)
- [交付检查与实际并行时段](delivery-checks.json)
- [新20题名单与前三批80题排除依据](selection-before-download.json)
- [冻结题面与初态配置](task-manifest.json)

本批真实主请求771次、辅助出站0次，12次辅助尝试在本地以input_token_bound_unknown拒绝，不计为出站。全部真实请求已结算；按配置价格和Provider usage估算41.63319080元，供应商实际账单未知。未用8.36680920元随本批关闭，不授权后续调用。出站输出限制、thinking、reasoning_effort及stream均未设置，不推定默认值；393216仅为费用预留。

复用既有4路并行执行器，镜像准备串行；环境不足的零模型题在前序闭账后首次执行，没有重跑付费失败题。第9/15题仅按[用户明确授权](memory-workspace-authorization.json)使用临时内存工作区；[镜像准备干预](memory-environment-intervention.json)保留固定官方内容与原始空间拒绝，[归档后清理记录](memory-workspace-cleanup.json)保留时序。

NZ-Coder Core准备基线28f2ec31679ceee410218c6371202ff7caed8ce9；官方公开集scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862。所有运行阶段360个Core文件及原生入口内容一致，期间的提交增量只涉及评估驱动、证据和导出回归。没有修改Core或官方验收器。

每题README和learning.md按实际请求→工具事实→补丁→独立验收提供复盘入口。脱敏请求/响应、工具与权限记录、实际改动及验收在仓库；未脱敏Provider原件、完整Session/Artifact保留本机，并在每题raw-identity.json/archive-receipt.json提供SHA-256。公开导出移除reasoning_content和凭据，不能称所有未脱敏原件都已上传GitHub。

本轮阶段排序回归：python -m pytest -q tests/evaluation/test_batch4_evidence_chronology.py，2 passed。未运行全仓或Windows；两项历史compaction fake失败继续保留，名称见最终报告。

私有原件目录：/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch4-2026-10-12。
