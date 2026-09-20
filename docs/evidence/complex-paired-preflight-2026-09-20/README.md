# 三个复杂任务的配对轨迹比较：执行前清单

状态：任务及初态已准备，真实模型尚未执行。上一授权明确限定一次 NZ N，不能覆盖本批。当前新增调用为 0；在收到本批明确授权前，不启动主模型、辅助模型或 embedding。

## 可审阅任务

| ID | 请求与难点 | 初态 | 独立验收重点 |
|---|---|---|---|
| M | 金额 API 迁移：不可变 Quote、精确十进制、币种、折扣、五个调用方及兼容旧入口 | 10 文件；原测试 1 passed | 先按行乘数量再 HALF_UP、JPY/USD/EUR、非法输入、空订单、原对象不变、五处迁移与中央计算、旧 API 保留 |
| Q | 异步工作池：有界并发、有序结果、失败收尾、取消及批量消费者 | 8 文件；原测试 3 passed | 真实重叠与及时补位、只调度一次、停止新工作但等待已启动任务、首错误/abort reason 身份、信号转交、监听器清理 |
| S | 配置存储升级：v1/v2 兼容、revision 冲突、严格校验、原子写入及两个调用方 | 6 文件；原测试 3 passed | 旧格式迁移、损坏文件保护、重复键、stale write、fsync/replace 故障注入、原字节与权限保留、临时文件清理 |

每题完整任务文本在 [manifest.json](manifest.json) 的 tasks.prompt；详细契约在各初态的 REQUIREMENTS.md。模型能看到全部功能要求。独立验收脚本位于 acceptance/，不复制进模型工作区；仅检查公开要求，不追加隐藏业务需求。

本机隔离子进程已确认三题“旧公开测试通过、新需求验收不通过”。M/S 的多数拒绝是所需新 API 缺失导致 fixture setup error；不能误称已有实现只失败一项。Q 的调度断言已能拒绝当前串行实现。上述检查证明初态非完成态，不等于验收器所有正负分支已获验证。没有预填正确补丁，也没有用替身模型生成所谓轨迹改善。

## 本批拟定调用范围

- 模型统一 deepseek-v4-flash；主请求不显式设置 thinking/reasoning_effort。
- NZ 固定 0885c874b949792723c515ff098c24d31cd238c9，走 NativeSDKRunner 生产入口。
- InfCodeX 固定 d3a812379b589597347f5be12d5b68477e577f02，走实际 SA 生产链。这一批将产生新的同期配对样本，不拿旧 N 充数。
- 三题 × 两侧 × 一次，共六次运行；每次最多 24 个实际主请求，总计最多 144 个主请求。
- 保留正常生产辅助调用，每次额外最多 8 个，总计最多 48 个。主/辅助各自预算耗尽则停止出站并如实记 budget admission blocked，不将其写作 Core 错误。重试算真实请求，不隐藏。
- 整批最多 192 个付费请求；这是授权上限而非必须用满。无自动重跑，失败也不增加轮次。
- 保留生产输出限制：NZ 64000，InfCodeX 历史有效值 32768；执行前和实际出站均核对。差异必须在报告中明示，不宣称纯粹隔离了 Agent Core 变量。
- 两侧使用同题同哈希初态、干净独立 HOME/会话、同 Node/Python；任务目录初始化相同 Git 基线供 diff 使用。只运行本地依赖，不安装依赖；网络隔离，模型只经记录代理。
- 测试命令均预先明确；执行采集器需核对两侧实际权限面。记录 approval/permission denied、dispatch failed 和真正 command failed，不能合成“测试失败”。若权限不能做到同等，明确标为生产产品比较的混杂因素，不声称完全等权。
- 付费服务不暴露实际账单时写 cost unknown；不推算美元。没有承诺金额硬上限。

这是新的复杂任务批次，不改变上轮 N 的 12 主请求限制，也不修改任何 Completion Core。

## 逐请求采集与评价

采集每次请求的 Provider 原字段、purpose、主/辅助累计、请求前预算、trigger、finish_reason、工具名/参数、执行事实、退出状态、permission、模型下一次实际看到的工具结果，以及 mutation/verification generation、requirement/contract、semantic verifier、terminal boundary。按每次写入保存文件版本，最终保存 diff、SHA-256、冻结独立验收与 usage。

日志副本删除 reasoning_content；live 请求原样传递。两侧不强行凑相同内部字段：InfCodeX 没有暴露或没有等价结构的 NZ ledger 标为 N/A，使用该侧实际工具、文件版本、事件及 terminal 原因解释。

最终先逐题判断哪个产品结果更好：

1. 独立功能验收和故障边界；部分通过按需求项列出，不只看一个 pass 总数。
2. 是否在预算内正常结束，以及有没有未完成却报告完成；正确代码和正常结束分开。
3. 轨迹差异：首次正确实现、首次可信测试、补丁后复测、无效工具参数试探、重复调查、权限阻塞、semantic 补救。
4. 上述质量相当时，再比较真实主/辅助用量和时间；cache input 不重复计数，失败样本不能凭省 token 算更好。

总结会给这三道任务中的胜负或各有所长；如果证据支持可以明确偏向某侧，但不把六次运行概括为普遍成功率或模型能力结论。所有观察发生前不预设胜者，不为了结果修改核心或提示。

## 离线复核与执行清单

本轮已运行：prepare.py（只创建任务、复制临时工作区和执行本地测试，不调用任何模型）。initial/ 与 acceptance/ 的 SHA-256 冻结在 SHA256SUMS。manifest.json 保存原测试和独立验收失败全文。

单题模型侧公开测试：

```text
M/S: python -m pytest -q tests
Q:   node --test tests/pool.test.cjs tests/batch.test.cjs
```

结束后独立验收在冻结副本上执行：

```text
env PYTHONPATH=FROZEN TASK_WORKSPACE=FROZEN python offline_exec.py python -m pytest -q -p no:cacheprovider acceptance/M.py
env TASK_WORKSPACE=FROZEN python offline_exec.py node --test acceptance/Q.cjs
env PYTHONPATH=FROZEN TASK_WORKSPACE=FROZEN python offline_exec.py python -m pytest -q -p no:cacheprovider acceptance/S.py
```

这些是命令形状；真实完整 argv、cwd、隔离环境均须落盘。获本批授权后只补采集/预算接线并检查，再执行六个真实样本。不得直接复用旧双侧 launcher 的 pooled 16 cap、原样落盘 reasoning 响应或旧 evidence 输出路径。

当前不提供两侧效果结论：真实复杂任务尚未运行。
