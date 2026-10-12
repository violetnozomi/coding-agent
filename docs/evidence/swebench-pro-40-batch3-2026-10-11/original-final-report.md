# 第三批固定 40 题：真实并发执行与闭账

40 个新实例排除前两批全部 40 题；29 题有真实主模型出站，官方独立验收 19 题通过、10 题未通过、11 题未知或未执行。SDK 启动 38 题，Agent 终态 {'completed': 9, 'max_turns': 13, 'error': 16}。SDK 启动但没有主模型请求不能计为自主编码任务。Agent 结束和冻结补丁验收分别保留，不事后改写 max_turns。

真实 DeepSeek Provider、NativeSDKRunner、文件工具和测试进程。官方来源固定 `scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862`。名单哈希 `a7f4e1750c24d430c7fcff55733b78151b7ad6eb845978f4082e397d16d79560`。

| 题号 / 复盘 | 项目 | 主 / 辅助请求 | Agent 终态 | 官方验收 |
|---|---|---:|---|---|
| [01](attempts/b3-01/README.md) | qutebrowser/qutebrowser | 19 / 0 | completed | 通过 |
| [02](attempts/b3-02/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 通过 |
| [03](attempts/b3-03/README.md) | ansible/ansible | 40 / 0 | max_turns | 未通过 |
| [04](attempts/b3-04/README.md) | ansible/ansible | 40 / 0 | max_turns | 通过 |
| [05](attempts/b3-05/README.md) | ansible/ansible | 37 / 0 | completed | 通过 |
| [06](attempts/b3-06/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 通过 |
| [07](attempts/b3-07/README.md) | tutao/tutanota | 40 / 0 | max_turns | 未通过 |
| [08](attempts/b3-08/README.md) | gravitational/teleport | 40 / 0 | max_turns | 通过 |
| [09](attempts/b3-09/README.md) | internetarchive/openlibrary | 40 / 0 | max_turns | 通过 |
| [10](attempts/b3-10/README.md) | qutebrowser/qutebrowser | 40 / 0 | max_turns | 通过 |
| [11](attempts/b3-11/README.md) | navidrome/navidrome | 32 / 0 | completed | 通过 |
| [12](attempts/b3-12/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 通过 |
| [13](attempts/b3-13/README.md) | gravitational/teleport | 29 / 0 | completed | 通过 |
| [14](attempts/b3-14/README.md) | internetarchive/openlibrary | 16 / 0 | completed | 通过 |
| [15](attempts/b3-15/README.md) | element-hq/element-web | 40 / 0 | completed | 通过 |
| [16](attempts/b3-16/README.md) | future-architect/vuls | 23 / 0 | completed | 通过 |
| [17](attempts/b3-17/README.md) | gravitational/teleport | 25 / 0 | error | 未通过 |
| [18](attempts/b3-18/README.md) | internetarchive/openlibrary | 40 / 0 | max_turns | 未通过 |
| [19](attempts/b3-19/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 通过 |
| [20](attempts/b3-20/README.md) | internetarchive/openlibrary | 40 / 0 | max_turns | 通过 |
| [21](attempts/b3-21/README.md) | internetarchive/openlibrary | 28 / 0 | completed | 通过 |
| [22](attempts/b3-22/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 通过 |
| [23](attempts/b3-23/README.md) | qutebrowser/qutebrowser | 7 / 0 | error | 未通过 |
| [24](preparation-retry-attempts/b3-24/README.md) | qutebrowser/qutebrowser | 21 / 0 | completed | 通过 |
| [25](preparation-retry-attempts/b3-25/README.md) | qutebrowser/qutebrowser | 11 / 0 | error | 未通过 |
| [26](preparation-retry-attempts/b3-26/README.md) | gravitational/teleport | 6 / 0 | error | 未通过 |
| [27](preparation-retry-attempts/b3-27/README.md) | protonmail/webclients | 0 / 0 | None | 未运行主模型 |
| [28](preparation-retry-attempts/b3-28/README.md) | protonmail/webclients | 0 / 0 | None | 未运行主模型 |
| [29](preparation-retry-attempts/b3-29/README.md) | flipt-io/flipt | 2 / 0 | error | 未通过 |
| [30](preparation-retry-attempts/b3-30/README.md) | ansible/ansible | 2 / 0 | error | 未通过 |
| [31](preparation-retry-attempts/b3-31/README.md) | flipt-io/flipt | 0 / 0 | error | 未运行主模型 |
| [32](preparation-retry-attempts/b3-32/README.md) | ansible/ansible | 0 / 0 | error | 未运行主模型 |
| [33](preparation-retry-attempts/b3-33/README.md) | qutebrowser/qutebrowser | 0 / 0 | error | 未运行主模型 |
| [34](preparation-retry-attempts/b3-34/README.md) | ansible/ansible | 1 / 0 | error | 未通过 |
| [35](preparation-retry-attempts/b3-35/README.md) | element-hq/element-web | 0 / 0 | error | 未运行主模型 |
| [36](preparation-retry-attempts/b3-36/README.md) | future-architect/vuls | 0 / 0 | error | 未运行主模型 |
| [37](preparation-retry-attempts/b3-37/README.md) | qutebrowser/qutebrowser | 0 / 0 | error | 未运行主模型 |
| [38](preparation-retry-attempts/b3-38/README.md) | gravitational/teleport | 0 / 0 | error | 未运行主模型 |
| [39](preparation-retry-attempts/b3-39/README.md) | flipt-io/flipt | 0 / 0 | error | 未运行主模型 |
| [40](preparation-retry-attempts/b3-40/README.md) | future-architect/vuls | 0 / 0 | error | 未运行主模型 |

主/辅助请求 819 / 0；返回 usage 的峰时费用估算 46.84200440 元，未知或其他保留 0.00000000 元，合计占用 46.84200440 元。总授权人民币 50 元，不沿用前批余额；供应商实际账单未知。剩余额度本批结束后关闭。

复用既有并发执行器，4 路任务共享宿主原子账本，镜像准备串行准入；各题独立工作区、HOME 和 Session。普通解题失败不停止其他题。付费失败题不重跑；仅零调用环境准备失败可在原预算剩余额度内补做。

每题最多 40 主请求、6 辅助请求、3000 秒。主请求不传 max_tokens/max_completion_tokens；393216 仅为费用预留，65536 为既有内部输出预留。实际 thinking/reasoning_effort/stream 是否设置逐请求核对，未设置不写成 medium/false。生产 Core 冻结不变，默认 policy 与显式 SWE 工具/权限集合保留；不是完全无约束终端配置。

容器无网络、非 root、隔离 HOME；密钥仅在宿主 Relay。公开请求/响应去除 reasoning_content 和凭据，完整原件、Session/Artifact 在私有归档保留，并公开 SHA-256/路径。补丁在 Agent 容器停止后冻结；新鲜原始项目镜像执行未修改的官方 verifier。未泄露隐藏验收，也未宿主代写补丁。原始单题归档配额 2 GiB；归档成功前不删除源目录。

准备阶段 urllib 传输慢，改用有界 curl，直连失败后才使用当前命令代理；已校验输入复用、名单不变、零付费请求。初次启动被旧 20 实例准入上限拒绝，未创建账本或启动 SDK；新增失败回归后调整为此次授权的 40 实例上限，不改变 50 元账本。

相关离线检查 67 passed（59 条模型转发/计费回归与 8 条既有并发续接回归），真实任务证据与离线检查分开。未运行全仓或 Windows。历史 compaction fake 失败 `test_context_overflow_stops_after_three_compaction_attempts`、`test_pre_send_and_reactive_compactions_share_one_three_attempt_owner` 未删除、弱化或重开专项。

每题保留题面、配置、请求、工具事实、补丁、改变文件、官方验收与复盘入口。冻结验收通过不能追认 Agent 未执行的测试。本批只报告固定样本逐题结果，不外推整体 SWE 成功率或并行提速倍数。

## 预算与环境边界

首阶段实测最多 4 个 Agent 重叠执行，补做阶段为单 worker，使用原授权剩余 4.00649704 元；各阶段代码和入口哈希一致。

发生本地预算拒绝的付费题为 17, 19, 20, 22, 23, 25, 26, 29, 30, 34。其中已通过的题仍按独立验收通过保留，未通过的题不能直接归因为模型修复能力不足。原始本地拒绝不是付费 Provider 调用；SDK 内部 provider_calls 等计数可能包括本地拒绝，真实出站以宿主账本为准。

零主请求题为 27, 28, 31, 32, 33, 35, 36, 37, 38, 39, 40。27 为官方镜像下载 EOF；28 为复制环境时磁盘保护中止，仅清理未启动 SDK、零模型请求的可重建半成品工作区，清理回执保留。其余为 SDK 已启动、预算预留拒绝所有请求；这些基线验收不是模型成绩。

剩余授权 3.15799560 元；单请求仅输出容量就保守预留 3.145728 元，再计必要输入。剩额无法通过这些请求的既有准入检查，不降低预留或输出能力，不借用旧批余额。尚未收到追加预算答复，11 题仍未完成模型执行。记录齐全不等于 40 题均已运行；当前停止新增付费请求。供应商实际账单未知。
