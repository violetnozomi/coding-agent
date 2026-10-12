# 八题A/B预冻结设计：尚未执行

A为`99eee2967ba8ff4dbfb17bf241e382b8fa022f5a`，B为`5e4f5f0cdd9451a612cc337f75d9d7eb4b936cd4`。这是一次事务版本修复的消融实验；不报告leaderboard pass@1，不把两侧更优补丁合并算单次结果。

名单来自官方公开Pro V2 `66f92766bba642462d4bbe5479e83f91f9211862`的642个题号。排除历史100题，对`SHA256("nz-coder-trace-audit-ab8-2026-10-12\n" + instance_id)`排序，取前8；[选择记录](ab8-selection-before-download.json)在下载题面之前写入，名单不因主题、准备难度或结果而替换。

|序号|新题|顺序|A/B验收|
|---|---|---|---|
|1|protonmail/webclients fc9d535e9beb3ae30a52a7146398cadfd6e30606|A→B|未运行/null|
|2|navidrome 6b3b4d83ffcf273b01985709c8bc5df12bbb8286|B→A|未运行/null|
|3|flipt aebaecd026f752b187f11328b0d464761b15d2ab|A→B|未运行/null|
|4|navidrome 87d4db7638b37eeb754b217440ab7a372f669205|B→A|未运行/null|
|5|tutanota de49d486feef842101506adf040a0f00ded59519 / v10a26bfb45a064b93f4fc044a0254925037b88f1|A→B|未运行/null|
|6|flipt ea9a2663b176da329b3f574da2ce2a664fc5b4a1|B→A|未运行/null|
|7|vuls 1832b4ee3a20177ad313d806983127cb6e53f5cf|A→B|未运行/null|
|8|protonmail/webclients 2dce79ea4451ad88d6bfe94da22e7f2f988efa60|B→A|未运行/null|

准确完整instance_id、16份题面/配置SHA与8份官方镜像digest都在[manifest](ab8-experiment-manifest.json)。只下载公开instruction.md/task.toml并读取registry清单；不读取新题的评分测试或参考补丁，不拉镜像，不调用模型。[题面下载回执](ab8-public-input-receipts.json)记录直连失败才使用现有代理的5次回退；[镜像身份](ab8-image-pins.json)8个均直连，含索引到Linux amd64产物的实际digest。

## 固定比较条件

复用现有[run-pro.py](../swebench-pro-20-2026-10-11/run-pro.py)的run_case、freeze和grade，A/B分别使用独立的OUT/code；相同native-entry.py、声明图MAIN_PROFILE、工具白名单及permission回调。旧的599次前缀拒绝条件保留，不趁消融改权限。repo policy保持原默认；不启用规划、reflection、embedding或新Reviewer。

模型deepseek-flash，stream=false；thinking/reasoning_effort未设置，不能写成显式medium。主请求wire max_tokens未设置；393216是费用预留量，不是输出限制。每次尝试40个实际主请求、最多6个辅助请求，Provider重试0，3000秒任务期限、600秒单请求期限，最多4个并行尝试，镜像准备串行。

沿既有Agent容器：2 CPU、4GiB、pids256、network=none、只读root、UID/GID1001、cap-drop ALL及no-new-privileges。只有受宿主准入控制的模型relay可出站，覆盖全部主/辅助/摘要边界。每对使用同一官方digest和衍生Agent环境；正式运行前核验衍生镜像与依赖清单，项目工具链不被Agent Python替换。

每次尝试独立工作区、HOME、会话、记忆及缓存；每对初态内容哈希一致。模型不可读取同题另一侧补丁、验收数据或gold patch。冻结实际终态补丁后再由同一评分器独立验收；成功、失败、阻断都保存，不追加“挑到成功”的重试。外部干预单列。

## 授权与停止条件

本轮没有新收费授权。总人民币额度、每次相等准入额度均为null，执行状态明确not-run；获得新授权后，必须在任何请求之前补充并冻结准入金额。既有保守预留、unknown usage与超预留止损保持，不沿用余额；预算不足留存未运行/未完成记录。

还需正式准备镜像/配对初态、冻结衍生环境身份、核实磁盘准入。旧两题/dev/shm授权不扩展到这些新题；空间不足时报告阻断，不偷偷换存储或题目。不要将本文件视为已经验证全部新题执行环境。

## 比较与解释

主要比较独立官方验收。分列进入handler与实际进程启动、具体拒绝/调度/命令失败、首次尝试/成功/提交修改请求、误STALE、回滚、无进展调用、实际主/辅助请求、usage、费用及耗时。未知保留null；工具成功、正常终止和官方验收各算各的。

先核实修复机制是否在新题触发；没有触发不推断有效或无效。更早修改、更少工具调用或相同请求上限不单独证明收益。公开8题小样本仅用于这一冻结研究对照，不推断全体SWE能力；本轮没有任何真实模型效果结果。
