# 第二批固定 20 题：真实执行与闭账

20 个新实例与第一批完全不重合；实际尝试 20 题，官方独立验收 17 题通过、3 题未通过。Agent 终态 {'max_turns': 10, 'completed': 10}。Agent 结束状态与冻结补丁的验收分别保留，不事后改写 max_turns。

真实 DeepSeek Provider、NativeSDKRunner、文件工具及测试进程执行；不是脚本化 Provider。官方固定 `scaleapi/SWE-bench_Pro-os@66f92766bba642462d4bbe5479e83f91f9211862`，固定名单哈希 `6be3614a91b9c70b9312c91c4d2b61dadecce1b2bee27e01cde531b49b6318ae`。

| 题号 / 复盘 | 项目 | 主 / 辅助请求 | Agent 终态 | 官方验收 |
|---|---|---:|---|---|
| [01](attempts/b2-01/README.md) | qutebrowser/qutebrowser | 40 / 0 | max_turns | 通过 |
| [02](attempts/b2-02/README.md) | navidrome/navidrome | 32 / 0 | completed | 通过 |
| [03](attempts/b2-03/README.md) | ansible/ansible | 40 / 0 | max_turns | 通过 |
| [04](attempts/b2-04/README.md) | flipt-io/flipt | 40 / 0 | max_turns | 未通过 |
| [05](attempts/b2-05/README.md) | navidrome/navidrome | 40 / 0 | max_turns | 通过 |
| [06](preparation-retry-attempts/b2-06/README.md) | future-architect/vuls | 40 / 0 | completed | 未通过 |
| [07](preparation-retry-attempts/b2-07/README.md) | flipt-io/flipt | 40 / 0 | completed | 通过 |
| [08](final-unattempted/b2-08/README.md) | protonmail/webclients | 40 / 0 | max_turns | 未通过 |
| [09](attempts/b2-09/README.md) | ansible/ansible | 31 / 0 | completed | 通过 |
| [10](attempts/b2-10/README.md) | protonmail/webclients | 40 / 0 | max_turns | 通过 |
| [11](attempts/b2-11/README.md) | flipt-io/flipt | 40 / 0 | completed | 通过 |
| [12](attempts/b2-12/README.md) | internetarchive/openlibrary | 39 / 0 | completed | 通过 |
| [13](attempts/b2-13/README.md) | element-hq/element-web | 40 / 0 | max_turns | 通过 |
| [14](attempts/b2-14/README.md) | ansible/ansible | 40 / 0 | max_turns | 通过 |
| [15](attempts/b2-15/README.md) | ansible/ansible | 40 / 0 | max_turns | 通过 |
| [16](attempts/b2-16/README.md) | internetarchive/openlibrary | 19 / 0 | completed | 通过 |
| [17](preparation-retry-attempts/b2-17/README.md) | protonmail/webclients | 40 / 0 | completed | 通过 |
| [18](attempts/b2-18/README.md) | gravitational/teleport | 30 / 0 | completed | 通过 |
| [19](attempts/b2-19/README.md) | internetarchive/openlibrary | 39 / 0 | completed | 通过 |
| [20](attempts/b2-20/README.md) | ansible/ansible | 40 / 0 | max_turns | 通过 |

真实出站主 / 辅助请求 750 / 0；usage 已知的峰时估算 37.96093664 元，未知或其他保留 0.00000000 元，本批占用 37.96093664 元。总授权 50 元，未沿用上一批余额；供应商实际账单未知。缺失 usage 不计零。所有阶段的逐笔结算、源码身份与文件哈希见 [闭账记录](final-accounting-and-integrity.json)。

补做准备目录过长曾导致 Unix 套接字绑定失败；两条原报告在 SDK 启动前设置了 attempt=1，但没有 launch.json 或模型请求。原件和旧导出保留、更正为零次实际 Agent 尝试；停止的准备阶段没有花费。修复仅在临时短路径建立同一 case 的别名，实际工作区与挂载身份不变。原实现的真实套接字回归失败、修复后通过，随后生产入口实际调用模型。

两个独立任务并行，共享同一阶段原子账本；镜像准备串行。第一阶段完全闭账后，只有零模型请求的准备失败题可在剩余额度内补做。大镜像按实测空间逐题释放；这是磁盘约束，不按成绩筛选。原准备失败记录仍在 attempts，实际补做记录单独保存；付费失败题没有重跑。

主请求不传 max_tokens/max_completion_tokens，393216 仅作费用预留。实际出站 stream、thinking、reasoning_effort 逐请求核对，不把未设置写成 false/medium。每题 40 主请求、6 辅助请求、3000 秒；同一 Core 快照、默认 policy、显式 SWE 工具/离线权限集合保留。不是完全无约束的终端默认配置。

Agent 无网络、非 root、隔离 HOME；密钥仅在宿主 relay。公开记录移除 reasoning_content 和凭据，私有原件保留请求/响应、Session/Artifact、冻结补丁和初终态。补丁冻结后，在全新原始项目镜像执行未改的官方 verifier；没有将隐藏验收反馈给 Agent，也没有宿主代写补丁。

08/09/10/17 的旧 512 MiB 原始归档配额失败单独保留；真实补丁和验收不变。所有连续原件及可重建索引完整保存到经逐文件校验的 2 GiB 有界归档，再释放本批临时源目录；没有因归档重跑模型。

第 08 题原始官方镜像下载超时，未启动 SDK、未调用模型。其 2,638,440,038 字节共享层和 1,519,764,643 字节最后一层从官方 GHCR 分段下载，逐段校验响应范围、拼接后验证完整 SHA-256；仅监听回环的临时缓存传输原样官方 manifest 和已核验层。第 08 题的本地 RepoDigest 主机名不同，内容摘要必须与官方 sha256:fc3c867ad010832886951359395ad2d3bcf31872b432317fa499d55eec0bc0cf 一致；第 17 题此前由原始 GHCR 下载完成，未使用此缓存。未修改系统 Docker 配置或项目文件，准备网络下载不计作模型调用。

相关离线回归：ModelRelay 58 passed，剩余额度续接和实际 Unix 套接字回归 6 passed。它们验证计费与控制流，不代替本次真实任务证据。没有重跑全仓或 Windows；两项历史 compaction fake 失败 `test_context_overflow_stops_after_three_compaction_attempts`、`test_pre_send_and_reactive_compactions_share_one_three_attempt_owner` 未删除、弱化或重新归因。

每题从请求 → 工具事实 → 补丁 → 官方验收复盘，README 给出实际命令与面试练习。冻结验收通过只证明其覆盖范围，不能追认 Agent 未执行的测试。第二批仅作固定小样本逐题记录，不报告总体 SWE 成功率、InfCodeX 对齐比例或并行提速倍数。

未通过项：04 的 Go 编译缺少新增依赖校验；06 的新增测试导入未声明模块而 setup failed；08 没有写工具或补丁，官方三组测试合计 8 failed / 5 passed。08 的 Bash 结果中 4 次未执行、3 次实际执行，均不是项目测试；08 的 40 个工具结果为 18 read_file、11 grep_search、2 list_directory、2 glob_search、7 Bash；需结合权限和输出复盘，不从工具数推断根因。
