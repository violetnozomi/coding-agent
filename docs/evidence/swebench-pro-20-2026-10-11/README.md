# SWE-bench Pro 20 题：任务与记录要求

状态：**准备完成，启动固定清单的真实模型单次尝试**。冻结清单不改写，实际结果另记；本页准备状态不是成绩。

## 本轮目标

- 使用 NZ-Coder 原生 Agent 完成 20 道 SWE-bench Pro 任务。
- 每题保留执行记录，包括失败、超时、取消和环境阻断，供复盘和面试学习。
- 原有 Verified/SymPy 受控校准不计入这 20 题。
- 冻结版本、题目清单与资源边界后运行；不根据结果换题，不追跑到成功。

## 已核实的入口与阻断

开始时 NZ-Coder HEAD：`98171657a6d2be083fed2d39c74f80138ddb98e5`，`main` 与远端同步，工作树干净。

`nz_coder/swebench/profiles.py` 目前仅注册 Verified/Lite；现有 `run_harness` 调用 `swebench.harness.run_evaluation`。它们不能仅换数据集名称便被称为 Pro 的官方评分入口。

2026-10-11 查询官方仓库 HEAD：`66f92766bba642462d4bbe5479e83f91f9211862`。这仅是查阅版本，不表示已下载、安装或执行评分器。

官方 [总说明](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/README.md) 区分 V1 与 V2；[V2 流程](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/README.md) 使用 Harbor 任务、隔离推理及新沙箱补丁复评。V1 脚本不是 V2 评分入口。

- Pro 版本：用户已选择 **V2 公开集**。[20 题名单](selected-ids.txt) 按公开任务 ID 的固定哈希顺序抽样，不参考答案、难度字段或成绩。
- 本轮 20 题总费用授权：用户回复 **“50元，deepseek-flash”**，即人民币 50 元，包含主模型及全部辅助调用。按用户明确请求名 `deepseek-flash` 记录，不沿用历史预算。尚未发送模型请求。
- 模型与单题请求、时间、输出上限：尚未冻结。
- 本机 Harbor：未安装；已有 swebench/datasets 不等于 Pro 评分环境已就绪。
- 本机可用磁盘：初查 `1,842,569,216` 字节，随后仅约 130 MiB。[首题镜像核验](image-preflight.json) 只读取匿名注册表 manifest：压缩层合计 `971,831,312` 字节，超过当时可用空间，未拉取镜像。解压、运行及完整记录还需要额外空间。
- 当前状态：**blocked: insufficient_persistent_storage**。用户授权只读盘点 `pyh` 目录；未删除缓存、历史证据或用户文件。目录盘点保留在私有证据位置，不公开无关项目路径。
- 本次新增付费请求：0。20 题均为 `not_run`，官方 resolved 为 `null`；这不是 20 次失败或 20 次尝试。

[任务 manifest](task-manifest.json) 与 [SHA-256](task-manifest.sha256) 保存固定题目、配置和题面哈希。20 份公开题面及 `task.toml` 原件留存于受控目录 `/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11/public-tasks`，均逐文件核对固定提交的官方 `v2/SHA256SUMS`。没有下载答案或隐藏验证正文。

官方 API 目录查询遇到 403 限流；改用官方校验和文件。raw 文件直连超时后，仅下载命令使用已有代理。镜像 manifest 经直连获取。上述准备网络请求不是模型调用。

## 每题必须保留的证据

复用现有请求记录、AttemptJournal、Session/Artifact 归档与补丁冻结机制；不以裁剪后的模型展示冒充原始输出。

1. 任务 ID、官方版本、公开题面、初态提交/哈希、执行环境与实际模型配置。
2. 主模型及全部辅助请求/响应，工具参数、实际执行结果、权限记录、完整可恢复失败证据与产物引用。
3. 实际测试命令、范围、退出状态及对应文件版本；运行终态与未知项如实保留。
4. 最终冻结补丁、文件 diff、哈希、独立官方复评的原始日志与结果。
5. 请求数、真实 usage、费用已知/未知项、耗时、人工干预与阻断原因。
6. 一份简短复盘：问题定位、修改依据、失败与恢复、复测结论、可用于面试讲解的知识点。不得把受控回放写成模型自主能力。

原始记录在受控位置保留；仓库同步脱敏的可审阅轨迹与证据索引，不公开密钥和私有 reasoning 内容。空间或预算不足时停止下一题并登记状态，不静默删记录。

## 交付口径

最终目录逐题对账，分别报告已尝试、未运行、环境阻断、Agent 结束状态及官方 resolved 结果。20 题是计划规模，不是当前已完成数量；未运行和缺失评分不能当成通过。

## 准备完成与正式运行口径

用户已授权清理三项可重建缓存，释放 7.58 GiB；没有清理其他项目或历史轨迹。首题官方镜像已真实拉取，其项目 Python 3.11.2、Go 1.24.3。Agent 依赖按原有锁定的 38 个版本、对应 ABI wheel 校验值安装；独立复评使用未加 Agent 依赖的原始镜像。

用户明确接受经验编码器计量风险：“接受，按现有止损机制启动”。预算仍为人民币 50 元，旧预算不沿用。现有 RelayLedger 的预留、缺失 usage/超预留停止机制不变；只增加本轮 Pro V2 20 个固定实例和 `deepseek-flash` 授权识别。官方当前模型名及价格依据为 [DeepSeek 价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：按较高时段输入 2、缓存输入 0.04、输出 8 元/百万 token 保守估计，估计不是账单。

真实入口为现有 `NativeSDKRunner → AgentRunner → ToolExecutor`；全部主/辅助调用经宿主现有 UDS 转发账本。每题主请求上限 40、辅助上限 6、3000 秒、单请求输出 8000；总请求上限 920、输入 1500 万、输出 250 万。模型 `deepseek-flash`，非流式，thinking/effort 未显式设置；实际出站字段和响应模型另存，不猜供应商默认。任务使用原生循环/工具/默认 policy；工具暴露为已有 SWE 允许集合，Shell 允许本地 Go/Node/Python 等项目工具。这是显式实验权限条件，不代表完全无约束生产默认。

Agent 使用无网络、非 root、只读容器根目录、独立 HOME 和工作区。真实密钥、私人配置及官方验收脚本不进入 Agent。仅本题隔离 `.bash_profile` 恢复镜像 PATH，避免登录 Shell 丢失 Go 路径；没有修改用户全局配置。任务补丁冻结前停止整个 Agent 容器；官方 `test.sh` 在新原始镜像上运行，校验值核对，验收文件不泄露给 Agent。

现有归档工具保存投影轨迹，另保留原始容器 Session、工具 Artifact、状态和权限的校验归档。HTTP 请求/响应原件分别保存，不能把投影当成完整请求。仅归档成功后回收本轮工作区/缓存及本轮镜像标签，不全局 prune。剩余空间低于 512 MiB 会停止正在执行的 Agent，下一题低于 1 GiB 不准入；未下载镜像先按公开层大小作保守空间检查，估计不是解压精确保证。

本次离线检查：真实首题容器中读取 `go.mod`、实际 Bash 返回 `go version go1.24.3 linux/amd64`、生产 SDK 正常终止、归档重新打开通过；3 次本地受控模型请求，0 次付费请求，不计入 Pro 成绩。首次离线检查的 Go PATH 失败及归档路径失败原件保留，不改写。相关计费回归 72 passed、3 skipped；指定实际 tokenizer/recipe 资源后计数回归 7 passed。跳过项是未选实际资源的 2 个编码测试及 1 个旧生产审查资源集成测试；本题实际容器入口另行跑过。未运行 Windows 或全仓测试。

执行命令：`python docs/evidence/swebench-pro-20-2026-10-11/run-pro.py --execute`。每题结果写入私有 `live/<序号>/report.json`，总状态 `live/progress.json`。未运行、环境阻断、Agent 状态、实际请求及官方 resolved 分开记录。受控 smoke 不证明真实模型编码能力。
