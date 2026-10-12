# 第三批固定40题：追加授权后的结果

实际主模型任务 40 题；官方独立验收 30 通过、10 未通过；未调用模型题号 []。未运行不计模型失败。

原授权已闭账估算 46.84200440 元；追加50元估算占用 18.19948592 元，余款不转为后续授权。供应商实际账单未知。

新增真实主/辅助请求 385/0，原主请求819、辅助0。只补零模型任务，原29题不重跑。被冻结的 Core 与原生入口哈希一致。

| 题号 / 轨迹 | 主请求 | Agent终态 | 官方独立验收 |
|---|---:|---|---|
| [01](attempts/b3-01/README.md) | 19 | completed | 通过 |
| [02](attempts/b3-02/README.md) | 40 | max_turns | 通过 |
| [03](attempts/b3-03/README.md) | 40 | max_turns | 未通过 |
| [04](attempts/b3-04/README.md) | 40 | max_turns | 通过 |
| [05](attempts/b3-05/README.md) | 37 | completed | 通过 |
| [06](attempts/b3-06/README.md) | 40 | max_turns | 通过 |
| [07](attempts/b3-07/README.md) | 40 | max_turns | 未通过 |
| [08](attempts/b3-08/README.md) | 40 | max_turns | 通过 |
| [09](attempts/b3-09/README.md) | 40 | max_turns | 通过 |
| [10](attempts/b3-10/README.md) | 40 | max_turns | 通过 |
| [11](attempts/b3-11/README.md) | 32 | completed | 通过 |
| [12](attempts/b3-12/README.md) | 40 | max_turns | 通过 |
| [13](attempts/b3-13/README.md) | 29 | completed | 通过 |
| [14](attempts/b3-14/README.md) | 16 | completed | 通过 |
| [15](attempts/b3-15/README.md) | 40 | completed | 通过 |
| [16](attempts/b3-16/README.md) | 23 | completed | 通过 |
| [17](attempts/b3-17/README.md) | 25 | error | 未通过 |
| [18](attempts/b3-18/README.md) | 40 | max_turns | 未通过 |
| [19](attempts/b3-19/README.md) | 40 | max_turns | 通过 |
| [20](attempts/b3-20/README.md) | 40 | max_turns | 通过 |
| [21](attempts/b3-21/README.md) | 28 | completed | 通过 |
| [22](attempts/b3-22/README.md) | 40 | max_turns | 通过 |
| [23](attempts/b3-23/README.md) | 7 | error | 未通过 |
| [24](preparation-retry-attempts/b3-24/README.md) | 21 | completed | 通过 |
| [25](preparation-retry-attempts/b3-25/README.md) | 11 | error | 未通过 |
| [26](preparation-retry-attempts/b3-26/README.md) | 6 | error | 未通过 |
| [27](memory-attempts/b3-27/README.md) | 40 | max_turns | 通过 |
| [28](memory-attempts/b3-28/README.md) | 40 | max_turns | 通过 |
| [29](preparation-retry-attempts/b3-29/README.md) | 2 | error | 未通过 |
| [30](preparation-retry-attempts/b3-30/README.md) | 2 | error | 未通过 |
| [31](supplement-attempts/b3-31/README.md) | 30 | completed | 通过 |
| [32](supplement-attempts/b3-32/README.md) | 40 | max_turns | 通过 |
| [33](supplement-attempts/b3-33/README.md) | 40 | max_turns | 通过 |
| [34](preparation-retry-attempts/b3-34/README.md) | 1 | error | 未通过 |
| [35](supplement-attempts/b3-35/README.md) | 40 | max_turns | 通过 |
| [36](supplement-attempts/b3-36/README.md) | 25 | completed | 通过 |
| [37](supplement-attempts/b3-37/README.md) | 24 | completed | 通过 |
| [38](supplement-attempts/b3-38/README.md) | 31 | missing_result | 通过 |
| [39](supplement-attempts/b3-39/README.md) | 39 | completed | 通过 |
| [40](supplement-attempts/b3-40/README.md) | 36 | completed | 通过 |

[原授权报告](original-final-report.md)、[原闭账账目](final-accounting-and-integrity.json)保持不变。新增账目见 [独立核验](supplement-accounting-and-integrity.json)。

公开请求/响应脱敏；完整原件及Session/Artifact保留本机并公开哈希。真实工具验证与独立官方验收分别记录，completed不等于验收通过。

未运行全仓或Windows，不报告整体SWE成功率、对齐比例或线上效率提升。历史compaction fake失败保留，预算/Core不扩建。

## 结束状态与交付边界


追加11题全部官方验收通过；两阶段385次真实主请求、0次真实辅助请求全部结算，完整JSON响应385份。原29题不重跑，合计40题有真实模型尝试、30题通过、10题未通过。原未通过任务中仍保留财务准入影响，不将全部未通过归因为编码能力。

原授权估算46.84200440元；追加授权估算18.19948592元；两次授权合计估算65.04149032元。追加授权未用31.80051408元在本批结束后关闭。供应商实际账单未知，不以这些数值替代账单，也不以token或时间宣称能力提高。

第38题外层达到3060秒边界，退出137，正式SDK结果未生成；保留missing_result和outer timeout。独立验收通过不反向证明最后写工具成功、Agent内测试通过或正常结束。唯一实际运行时journal按原字节恢复完整行，末尾未补造事件、工具结果或终态。

9题新增真实运行与旧SDK零调用记录的归档目录重名，原delivery error保留，离线用独立阶段名称补档；27/28内存工作区投影触发原直接子目录校验，验证已授权真实路径后为原校验提供实际工作区根。安全校验器未修改。全部补档不调用模型、不复测、不修改任务代码。两题源码只在Session/Artifact归档收据生成后清理。

4路为小题阶段调度上限；大镜像依既有资源顺序及用户授权串行，2题实际在内存工作区运行。准备暂停、内存位置选择及离线归档修复均记录为环境/交付干预，未向模型提供正确补丁或隐藏验收。初始harness启动时尚有局部未提交改动，执行配置保留当时HEAD；Core与入口通过完整文件哈希字典核对，不能仅用source_commit推断harness每个字节。

本轮续跑、公开导出、真实Docker及真实归档边界回归12 passed，最终命令和失败到通过记录见 [回归记录](supplement-local-tests.txt)。原67条检查不重复运行；全仓和Windows未运行。原两项compaction fake失败仍单列，不删除或弱化。
