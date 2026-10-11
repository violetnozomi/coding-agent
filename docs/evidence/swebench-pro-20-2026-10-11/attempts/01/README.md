# 第 01 题：RHEL 衍生发行版版本号归一化

真实 `deepseek-flash` 自主执行，16 次主请求、0 次出站辅助请求；2 次辅助请求在本地计数准入阶段被拒绝。按峰时单价和实际 usage 估算 0.1620748 元人民币，不是账单。Agent completed，冻结补丁在全新原始镜像上运行未改写的官方 verifier，reward=1、退出码 0，即本题官方 resolved=true。

模型通过 grep/read 找到 `oval/util.go` 与已有测试，使用原生 apply_patch 将 utility 和调用点改名，同时更新原有测试调用。权限回调拒绝了 3 次以 cd 开头的 Shell 请求；首次静态编译遇到容器进程数限制。模型自行使用 workdir 参数与直接 Go 命令推进，目标归一化、lessThan 和影响范围测试实际通过。宿主没有提供补丁或纠正建议，也没有重跑模型。

[请求](provider-requests.jsonl)、[响应](provider-responses.jsonl)按真实请求排序，已删除公开 reasoning_content；完整原件及哈希索引保存于私有根目录 live/01/private-provider。工具的实际执行、拒绝、错误、通过和版本可从 [runtime](runtime.jsonl)核对。Session 与可恢复 Artifact 原件见 [归档索引](archive-receipt.json)。[补丁](model.patch)和[官方独立结果](evaluation-result.json)分开记录。

复盘/面试要点：先区分接口改名和算法变更；追踪函数的全部调用及测试引用；测试工具执行成功不等于其中所有检查通过；环境编译失败不能冒充断言失败；Shell 的 pipefail、workdir 和权限口径会影响验证。官方通过只证明官方所测范围，未额外验证 `.esN` 文本的规范化；补丁保留了原 `[es]l` 正则，不能宣称全部文字需求已有独立覆盖。

所有受控 smoke 排除于本题结果；原始官方验收脚本未进入 Agent。没有人工编辑任务文件，没有读取参考答案。
