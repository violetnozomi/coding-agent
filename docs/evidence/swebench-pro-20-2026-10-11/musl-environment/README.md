# 第 08 题环境准备失败与恢复

正式 Agent 尚未启动，模型请求为 0。原镜像 Python 3.12.10、pip 24.0，实际兼容标签为 musllinux；原准备流程固定下载 manylinux 轮包，构建在 charset-normalizer==3.5.2 处失败。原日志保留，不将其算作模型或补丁失败。

只调整现有 run-pro.py 的 Agent 依赖准备：读取原镜像 pip 的兼容标签，选择对应 musllinux 轮包；38 个依赖版本保持不变，下载后的 SHA-256 固定。安装位置仍为隔离 /opt/nz-site，不更换项目 Python，不修改 Core。原 manylinux 缓存未覆盖。

实际 Docker 构建通过；真实 NativeSDKRunner 在该镜像读取 go.mod，并真实执行 go version，随后 completed，文件未改。3 次受控本地请求均不带 max_tokens，0 次收费请求。原始工具/Session/Artifact 归档在私有 mu 目录，检查结论见 runtime-smoke-verification.json。此检查不是第 08 题成绩。

当前长运行协调器已加载旧准备函数，源文件变化不会悄悄改变其在途任务。第 08 题留待本队列结束后，核对全部已结算费用与零次尝试事实，再进行首次 Agent 运行。
