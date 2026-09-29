# 独立 CubeSandbox 实验流程

正式入口统一为 `clawbox experiment`。它直接使用 standalone CubeSandbox
运行 Runtime VM 和 Tool VM，不依赖 Kubernetes，也不需要人工拼装 cgroup、
NUMA 或镜像参数。

## 1. 一次性配置实验主机

在 ARM64 Linux/KVM 主机上执行：

```bash
clawbox experiment setup \
  --runtime-template RUNTIME_TEMPLATE_ID \
  --tool-template TOOL_TEMPLATE_ID \
  --node NODE_IP \
  --cube-source "$HOME/src/CubeSandbox" \
  --local-gib 36 --low-gib 28 --high-gib 32 \
  --warm --warm-gib 128 --shared-borrow-percent 50
```

这套配置的语义是：

- 实验可配置的 VM 容量由 spec 决定；c16、每个 agent 6 GiB 时为 96 GiB。
- NUMA0 的 LOW/HIGH/HARD 为 28/32/36 GiB。32 GiB 是控制目标，不是失败线；
  短暂超过 HIGH 会被测量，但不会判失败。
- A+B+C 在超过 HIGH 后把安全等待的 VM 快照迁移到 NUMA1 的 WARM 池，
  直到 LOCAL 回落到 LOW。
- 接近 HARD 时，仍在运行的完整 VM cgroup 可以临时借用 NUMA1。借用按 VM
  配置容量预留，上限为共享池的 50%，即 64 GiB。
- NUMA1 的实时借用和快照共同计入 128 GiB 共享池；父 cgroup 的组合上限为
  36+64=100 GiB，但 NUMA0 本身仍以 36 GiB 为硬边界。

`setup` 会启动并检查 CubeSandbox 服务，配置 cgroup 与 WARM tmpfs，创建、
执行并销毁探测 VM。全部通过后才原子写入
`~/.config/clawbox/host.json`。它不会停止未知 VM 或删除已有结果。

升级 ClawTune 后首次重建镜像时执行：

```bash
clawbox experiment images \
  --registry REGISTRY/clawbox \
  --go /path/to/go \
  --kernel-source /path/to/linux-source \
  --kernel-build /path/to/linux-build \
  --direct-network
```

命令会保存非敏感构建参数。后续更新只需执行：

```bash
clawbox experiment images
```

## 2. 用命令生成实验配置

先查看正式支持的三个方案：

```bash
clawbox experiment baselines
```

| 方案 | baseline | 行为 |
| --- | --- | --- |
| A | `tool-static-resident` | 固定内存准入，VM 常驻 |
| A+B | `tool-p50-resident` | 命令级 P50 预测准入，VM 常驻 |
| A+B+C | `tool-p50-wait-reactive` | P50 准入，并对安全等待 VM 做 WARM 快照迁移/恢复 |

训练 run 只运行 A，并记录真实 CubeSandbox guest memory：

```bash
clawbox experiment configure examples/experiments/getting-started.yaml train.yaml \
  --experiment-id train-memory \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --baseline tool-static-resident \
  --concurrency 16 \
  --pool-memory-gib 32

clawbox --output-root /data/clawbox-results \
  experiment run train.yaml --run-id train-01

clawbox experiment train /data/clawbox-results/train-01 \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --output /data/clawbox/predictions/eval-p50.json
```

评估配置由 `configure` 自动读取主机 profile 中的节点、模板、镜像 digest、
LOCAL/WARM 容量，不需要用户手工复制这些字段：

```bash
clawbox experiment configure examples/experiments/getting-started.yaml eval.yaml \
  --experiment-id memory-overcommit-eval \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --baseline tool-static-resident \
  --baseline tool-p50-resident \
  --baseline tool-p50-wait-reactive \
  --prediction-artifact /data/clawbox/predictions/eval-p50.json \
  --static-tool-memory-mib auto \
  --concurrency 16 \
  --pool-memory-gib 32 \
  --snapshot-storage warm-only \
  --model-wait-prediction-seconds auto
```

`auto` 会把训练数据与 trace 中的校准值冻结到 spec；正式运行中不会在线修改
预测模型。LatticeKB 无有效结果时，ClawBox 回退到 ToolKB；缺失的内存观测不会
被伪造为 0。

## 3. 正式运行前门禁

```bash
clawbox experiment validate eval.yaml --inputs
clawbox experiment describe eval.yaml
clawbox experiment doctor eval.yaml --probe-vm
clawbox --output-root /data/clawbox-results experiment qualify eval.yaml
```

资格验证会以目标并发在三个方案上真实创建 VM，验证 OpenClaw、预测、Tool
遥测、快照/恢复、NUMA1 借用、监督器和清理。成功 receipt 与 spec、源码实现、
并发、节点、模板及镜像 digest 绑定；这些内容发生变化后必须重新验证。

## 4. 运行、恢复与报告

```bash
clawbox --output-root /data/clawbox-results \
  experiment run eval.yaml --run-id eval-01 --detach

clawbox --output-root /data/clawbox-results experiment status eval-01
clawbox --output-root /data/clawbox-results experiment report eval-01
```

中断后只重跑未成功的 arm：

```bash
clawbox --output-root /data/clawbox-results experiment resume eval-01 --detach
```

需要主动停止或删除该 run 拥有的 VM 时：

```bash
clawbox --output-root /data/clawbox-results experiment abort eval-01
clawbox --output-root /data/clawbox-results experiment destroy eval-01
```

`status` 读取原子更新的 `run-state.json`。每个 arm 都由独立 worker 和硬截止
时间保护；`resume` 只跳过 spec digest 匹配、结果成功且清理已验证的 arm。

## 5. 如何判断结果有效

一个 arm 只有在所有 session 完成、Tool 遥测完整关联、结果验证通过、无 OOM、
共享池未越界且所属 VM 全部清理后才算成功。32 GiB 以上的短暂 LOCAL 超调是
实验指标，不是失败；应报告持续时间和 GiB-seconds。36 GiB 是 NUMA0 边界，
超过它的活跃需求必须由受限的 NUMA1 借用吸收。

结果目录包含：

| 文件 | 内容 |
| --- | --- |
| `run-state.json` | 实时/终态以及每个 arm 的状态 |
| `experiment.yaml`, `qualification.json` | 冻结配置与资格 receipt |
| `summary.json`, `summary.csv`, `summary.md` | 三方案汇总 |
| `report.md` | 可直接引用的吞吐、JCT、准入、内存、借用和迁移比较 |
| `memory-timeseries.csv` | LOCAL、共享借用、WARM 与整机内存时间序列 |
| `attempts/ATTEMPT/ARM/` | 事件、日志、gateway、遥测与 ownership 原始证据 |

报告时应分别解释 96 GiB 配置容量、准入预测/预留、NUMA0 实测占用、NUMA1
实时借用、快照空间和整机内存，不能用 VM 配置容量代替物理内存测量。
