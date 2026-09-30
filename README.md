# ClawBox

ClawBox 在 standalone CubeSandbox 上比较编码 Agent 的内存准入、空闲 VM 回收和恢复策略。
每个会话包含一个 Runtime VM 和一个 Tool VM；ClawTune 提供命令级预测与测量。
默认用 NUMA 0、1 模拟两个计算节点，NUMA 2 提供共享内存池。

**从[逐条命令入门](docs/self-service.md)开始。** 正式入口是 `clawbox experiment`，
不需要 Codex、代码代理或 Kubernetes。真实执行需要 ARM64 Linux/KVM 主机、
带补丁的 CubeSandbox 和配套 Guest 构件；仓库不包含可直接运行的完整 Guest 镜像。

| 你要做什么 | 阅读位置 |
| --- | --- |
| 配置主机，跑通第一个实验，再训练和评估 | [入门流程](docs/self-service.md) |
| 从新机器安装服务、导入镜像、注册模板、更新 ClawTune | [安装与更新](docs/installation.md) |
| 修改 NUMA、CPU 子集、内存、水位、共享池或磁盘 | [主机与超节点配置](docs/supernode.md) |
| 查实验字段、trace 要求、命令区别、恢复和结果含义 | [配置与命令参考](docs/guide.md) |
| 理解准入、快照、计账及 PMU 的实现边界 | [执行与测量契约](docs/design.md) |
| 开展独立的 LLC 放置研究 | [辅助放置工具](docs/placement.md) |

[getting-started.yaml](examples/experiments/getting-started.yaml) 和
[smoke.jsonl](examples/traces/smoke.jsonl) 是最小入门输入；训练示例使用
[memory-smoke.jsonl](examples/traces/memory-smoke.jsonl)。其他 experiment/prediction 文件
是研究输入，可能含机器路径、占位模板或旧策略，不是新机器默认配置。

开发者运行相关检查：`python -m pytest`。公共参数以 `clawbox experiment --help`
及各子命令的 `--help` 为准；字段定义见 [spec.py](clawbox/experiments/spec.py)。
修改公共行为时同步更新对应参考页，完整命令流程只维护在入门页。

旧控制面和 `scripts/lab` 的范围见[弃用说明](docs/deprecated.md)。
[历史实验记录](docs/archive/README.md)只保留当时的配置、结果与局限，不表示当前部署状态。
