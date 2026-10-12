# 三个带原事件的面试案例

## 1. 假陈旧错误掩盖真锚点错误：b4-09

第36次实际主请求连续编辑同一Metrics.ts。[runtime756](../swebench-pro-20-batch4-2026-10-12/final-unattempted/b4-09/runtime.jsonl#L756)的`call_00_2EIZeFKys7BxVJPCrtEU2034`成功；[757](../swebench-pro-20-batch4-2026-10-12/final-unattempted/b4-09/runtime.jsonl#L757)的`call_01_FckAKA15905pqyocUa8t1809`和[758](../swebench-pro-20-batch4-2026-10-12/final-unattempted/b4-09/runtime.jsonl#L758)的`call_02_XyH2N7wup2pju49QSeXK0718`被STALE_READ拒绝，后续回滚。模型在第37个[真实请求](../swebench-pro-20-batch4-2026-10-12/final-unattempted/b4-09/provider-requests.jsonl#L37)才看见拒绝。

不能一看到STALE就认定外部人改文件：前序自身写入尚未刷新模型基线。通过无外部修改、唯一锚点、串行执行的真实Runner反例排除竞争解释，再复用事务票据区分自身V2与外部V2。

严格限制结论：录制回放证明第三个原锚点不匹配。修复后的正确行为是第二项执行、第三项真实EDIT_NOT_FOUND并回滚，给模型准确候选；不是全批成功。该题#35才起修改、hook/机制选择/数据传递遗漏仍然存在。可以说明修复了生产控制流，不能说明修复了整题或提高真实成功率。

## 2. 功能验收与诚实结束分别核实：b4-02

#11的[真实文件创建](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/runtime.jsonl#L238)实现ISBNdb提供器；#15的`call_00_YAKrxowZmvb4W0nKfWXD5414`实际运行[目标pytest](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/runtime.jsonl#L332)，17 passed；#17 [ruff](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/runtime.jsonl#L377)、#18 [静态检查](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/runtime.jsonl#L399)随后完成；#21的[响应](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/provider-responses.jsonl#L21)为stop。

最终状态记录mutation_generation=verification_generation=3；[官方验收](../swebench-pro-20-batch4-2026-10-12/preparation-retry-attempts/b4-02/evaluation-result.json)通过。功能通过、当前版本验证和正常终止各有证据。辅助审查发生了本地拒绝，0个真实辅助请求；不能把成功归因于Reviewer模型。空任务合同也不是“全部自然语言需求已证明”。

## 3. 环境失败、测试错误与实现错误分开：b4-19

#7的`call_00_DjIn4cvPK7lO3OVgLxOY0791`真正运行[Go目标测试](../swebench-pro-20-batch4-2026-10-12/attempts/b4-19/runtime.jsonl#L153)，失败来自newosproc资源限制。#12降低并行后，`call_00_FjZlsneP1HhvcnvaDnA76790`得到[真实断言失败](../swebench-pro-20-batch4-2026-10-12/attempts/b4-19/runtime.jsonl#L254)expected1/actual2；这次来自新加测试的预期。#14 [修改测试预期](../swebench-pro-20-batch4-2026-10-12/attempts/b4-19/runtime.jsonl#L294)，#15的`call_00_8JJMdUrlRqDIGzPw4FQ71730`[目标输出ok](../swebench-pro-20-batch4-2026-10-12/attempts/b4-19/runtime.jsonl#L318)，最终[官方验收](../swebench-pro-20-batch4-2026-10-12/attempts/b4-19/evaluation-result.json)也通过。

不能把每次非零退出都叫“代码未修好”，也不能把修正测试预期叫再次修复生产实现。该命令通过pipe tail，运行时没有将它记为可靠当前版本正向验证；这是已有保护，不能为了漂亮完成数字取消。目标stdout、退出状态、验证状态和独立功能验收分列。
