# SWE-bench Pro V2 第三批：40 道新题

本批授权人民币 50 元，模型 `deepseek-flash`。不沿用前两批余额；所有主/辅助请求共用账本。

按既有固定公开题目 ID 哈希顺序选择接下来的 40 题，排除前两批全部 40 题，不按成绩换题。官方来源固定 `scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862`。

计划 4 路 Agent 并发，镜像准备串行准入。每题最多 40 个主请求、6 个辅助请求、3000 秒，主请求不传 max_tokens；费用保守预留继续按已有经验计量与止损机制。总预算可能不足以完成全部题目，未尝试项必须保留为未执行，不能算测试失败。

Agent 复用真实 NativeSDKRunner、隔离容器和原生工具；网络仅可经宿主受控模型转发器，容器不含密钥。隐藏官方验收不进入 Agent 环境，补丁冻结后使用新鲜原镜像独立验收。

原始含推理内容的 Provider 记录、Session/Artifact 原件保留在 `/home/pyh/.codex/artifacts/nz-swebench-pro-v2-40-batch3-2026-10-11`。公开导出移除凭据和 reasoning_content，保留原件 SHA-256、工具事实、补丁及验收。正在执行；已结束题目见 [当前记录](current-outcomes.json)。本文件不代表全部已完成。
