# 独立完成 CubeSandbox 实验

正式入口只有 `clawbox experiment`。它直接使用 standalone CubeSandbox，不需要
Kubernetes。一次 run 会创建所需的 Runtime/Tool VM，逐个执行 arm，并销毁该
arm 拥有的 VM；不会保存新的大 VM 镜像。`scripts/lab` 旧入口已经弃用。

## 1. 配置主机

在 ARM64 Linux/KVM 主机上执行：

```bash
clawbox experiment setup \
  --runtime-template RUNTIME_TEMPLATE_ID \
  --tool-template TOOL_TEMPLATE_ID \
  --node NODE_IP \
  --cube-source "$HOME/src/CubeSandbox" \
  --local-gib 64 \
  --warm --warm-gib 128
```

启用 WARM 时，`setup` 会检查当前 Python SDK；如版本不匹配，它只把本仓库的
tiered/incremental SDK 补丁应用到 `--cube-source`，再安装该 SDK。成功后源码
路径会写入主机配置，后续执行 `setup` 无需重复传入。

`setup` 启动已安装的 CubeSandbox 服务，配置 LOCAL memory cgroup，并在启用
`--warm` 时配置 tmpfs WARM pool。它会创建、执行、销毁一台探测 VM，全部
通过后才保存 `~/.config/clawbox/host.json`。它要求空闲主机，不会停止未知 VM，
也不会清理实验目录、模板或镜像。

升级 ClawTune 后，可重建 Runtime/Tool 集成并生成新模板。该命令同时把固定版本的
ARM64 mvdan 命令解析器编译进镜像，正式运行不会临时下载编译器：

```bash
clawbox experiment images \
  --registry REGISTRY/clawbox \
  --go /path/to/go \
  --kernel-source /path/to/linux-source \
  --kernel-build /path/to/linux-build \
  --direct-network
```

首次成功后，命令会把 registry、Go 可执行文件、客户机内核源码/构建目录和网络模式写入
`~/.config/clawbox/host.json`。后续升级 ClawTune 时只需执行：

```bash
clawbox experiment images
```

如果需要重新使用代理，执行一次 `clawbox experiment images --proxy-network`，新的选择会在成功后保存。
密码和代理凭据不会写入主机配置。

把新模板 ID 和不可变镜像 digest 写入实验 YAML。旧模板不会被删除。

## 2. 准备配置与 trace

查看正式 baseline：

```bash
clawbox experiment baselines
```

当前正式比较只有：

- `tool-static-resident`：A，校准后的固定内存准入，VM 常驻。
- `tool-p50-resident`：A+B，使用 P50 预测准入，VM 常驻。
- `tool-p50-wait-reactive`：A+B+C，P50 准入加等待期 WARM checkpoint/restore。

旧策略仍可用 `clawbox experiment baselines --all` 查看，但均标为
`DEPRECATED`，不进入新实验。

从完整 CubeSandbox 训练 run 生成 P50 预测：

```bash
clawbox experiment train RESULT_DIR \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --output /data/p50.json
```

预测优先使用 LatticeKB，无法给出结果时由 ClawBox 回退到 ToolKB。这个回退
只在 ClawBox 中实现。没有测量依据的缺失值不会被写成 0。

包含 A+B 或 A+B+C 的正式配置必须通过 `--prediction-artifact /data/p50.json`
引用这份冻结产物。这样评估开始前已经固定每个命令使用 LatticeKB、ToolKB
回退或有测量依据的短调用假设；运行中不会学习评估数据。

`configure` 默认读取 `~/.config/clawbox/host.json`，自动填入 setup/images 生成的
模板 ID、镜像 digest、节点、LOCAL cgroup 和 WARM 容量。选择 A+B+C 时增加
`--snapshot-storage warm-only`，即可明确要求使用 tmpfs WARM；不允许静默回退到磁盘。

如果输入是受支持的研究 trace，先转换为当前 schema 6：

```bash
clawbox experiment import-trace old.jsonl \
  --output replay.jsonl \
  --python /opt/conda/envs/testbed/bin/python
```

随后检查完整输入和资源形状：

```bash
clawbox experiment validate experiment.yaml --inputs
clawbox experiment describe experiment.yaml
clawbox experiment doctor experiment.yaml --probe-vm
```

`doctor` 会核对服务、KVM、模板及镜像 digest、LOCAL memory.max、NUMA、
WARM tmpfs 容量和当前 ClawTune revision。配置不符时不会启动正式 run。

## 3. 先做短资格验证

每个正式配置必须先通过同一目标并发的短验证：

```bash
clawbox --output-root /data/clawbox-results \
  experiment qualify experiment.yaml
```

资格验证使用 replay trace 的第一个模型步骤（该步骤必须含 Tool 调用），在配置的最大并发
和全部正式策略上真实创建 VM、执行 OpenClaw、采集 Tool 遥测并验证清理。若配置含
快照策略，它还会额外创建一台 Tool VM，按配置执行 checkpoint、restore、恢复后命令和
销毁往返。它同时故障注入一个卡住的 worker，确认主机可以杀死整个进程树。成功后在
YAML 旁生成 `experiment.yaml.qualification.json`。receipt 绑定完整 spec digest、
并发、策略、节点和两个镜像 digest；任一项改变都必须重新资格验证。

## 4. 运行、观察和恢复

前台运行：

```bash
clawbox --output-root /data/clawbox-results \
  experiment run experiment.yaml --run-id formal-01
```

后台运行：

```bash
clawbox --output-root /data/clawbox-results \
  experiment run experiment.yaml --run-id formal-01 --detach
```

查看真实运行状态：

```bash
clawbox --output-root /data/clawbox-results experiment status formal-01
```

状态来自原子更新的 `run-state.json`，包含 supervisor PID 身份、当前 arm、阶段、
心跳、事件文件进度和每个 arm 的清理结论。`running` 但 PID 身份失效会显示为
`orphaned`，不再以“没有 summary”推测进程仍在运行。

中断或机器重连后：

```bash
clawbox --output-root /data/clawbox-results experiment resume formal-01
```

resume 只跳过 spec digest 匹配、结果成功且清理已验证的 arm。失败或超时 arm
使用新的 attempt 目录重跑，旧 attempt 证据保留。

主动停止和清理：

```bash
clawbox --output-root /data/clawbox-results experiment abort formal-01
clawbox --output-root /data/clawbox-results experiment destroy formal-01
```

清理只依据本 run 的 task UID、Cube metadata 和持久 ownership journal。清理本身
在有硬超时的独立进程中执行；无法确认 VM 全部消失时，run 会失败并保留明确的
`cleanup_verified: false`，不会继续下一个 arm。

## 5. 结果成立条件

一个 arm 只有同时满足以下条件才标记为 `succeeded`：模型响应完整交付、所有
Tool 调用完成、最终验证通过、Tool 遥测按 execution ID 完整关联、没有 host OOM
或 pool 越界，并且拥有的 VM 已全部清理。父 supervisor 为每个 arm 设置硬截止
时间；线程或 SDK 调用卡住时会终止整个 worker 进程树。

结果位于 `RUN_ID/summary.json`、`summary.csv`、`summary.md` 和 `arms/`。每次
尝试的完整日志、事件、gateway 状态和 ownership journal 位于
`RUN_ID/attempts/ATTEMPT_ID/ARM_ID/`。
