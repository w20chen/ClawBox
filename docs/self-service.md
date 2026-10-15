# 自助配置与实验操作

以下命令在目标 **ARM64 Linux/KVM 主机的 Bash 终端**中执行。可以先 `ssh kunpeng`，
再进入该机器的 ClawBox 目录。命令按顺序执行；任何一步失败，先按提示修复。

| 场景 | 从哪里开始 |
| --- | --- |
| 全新机器，没有服务、镜像或内核 | [安装指南](installation.md)，完成后回到这里 |
| 已安装，换终端继续工作 | “已有环境” |
| 有 host.json，缺少可编辑配置 | `host init --from-profile` |
| 刚重启或修改 NUMA/容量/存储 | “检查并应用主机配置” |
| 首次实验或更新后验收 | “最小实验” |
| 训练、策略比较 | “训练与策略对比” |
| SSH 断开、停止或恢复 | “后台运行与恢复” |

## 已有环境

```bash
cd "$HOME/ClawBox"  # 改成实际 checkout；新安装示例是 $HOME/src/ClawBox。
bash scripts/clawbox experiment --help
bash scripts/clawbox experiment host inspect
```

`scripts/clawbox` 自动选择项目 `.venv/bin/python`，加载
`~/.config/clawbox/machine.env`，无需 activate 或 `set -a`。需要管理员权限时加
`--sudo`；脚本在提权前加载配置并保留用户目录，避免读取 `/root` 的另一套配置。
直接使用 Python 包安装的 `clawbox` 命令时，仍需自行加载环境。

其他配置或 Python 环境：

```bash
bash scripts/clawbox --machine-env /absolute/path/machine.env experiment host inspect
CLAWBOX_PYTHON=/absolute/path/venv/bin/python bash scripts/clawbox experiment --help
```

环境文件是可信 Bash 文件，其赋值覆盖同名 shell 变量；CLI 参数覆盖环境默认值。
`--machine-env`、`--sudo` 放在普通 CLI 参数之前。指定的文件缺失会报错。

Python 环境缺失或需要更新时，换成实际源码路径再执行：

```bash
bash scripts/bootstrap-python.sh --python python3.12 \
  --cube-source /absolute/path/CubeSandbox --clawtune /absolute/path/ClawTune
```

脚本检查版本和路径、创建或复用 `.venv`、安装依赖和指定 SDK/ClawTune，最后执行
`pip check`；网络需要绕过继承的代理时显式加 `--direct-network`。不替换主机服务或重建镜像。ClawTune 更新后主机与两个 VM 镜像必须匹配。

## 配置文件与主机初始化

| 文件 | 内容 |
| --- | --- |
| `~/.config/clawbox/machine.env` | API/代理地址、guest 可访问的主机地址、源码路径、镜像摘要、模板 ID、默认结果目录 |
| `~/.config/clawbox/host.yaml` | NUMA、CPU 集合、本地容量/水位、共享池和磁盘路径 |
| `~/.config/clawbox/host.json` | apply 成功后自动生成，供 configure 使用，无需手工编辑 |
| `$HOME/clawbox-specs/*.yaml` | 实验负载、并发、重复次数、策略、快照方式、超时和预测文件 |

已有 `host.yaml` 时直接使用它。缺少时选择下面一种方式；初始化不会覆盖文件。
新主机从实际拓扑和 machine.env 提出配置：

```bash
bash scripts/clawbox experiment host init "$HOME/.config/clawbox/host.yaml" --detect
${EDITOR:-vi} "$HOME/.config/clawbox/host.yaml"
```

`--detect` 选择前两个至少 16 GiB 且有 CPU 的 NUMA 节点计算，存在第三个可用节点时
启用共享池，否则设置 `warm: false`。计算域建议容量为物理容量四分之一且最多 36 GiB；
共享池最多 128 GiB。**初始预算不代表实时可用内存或负载需求，应用前须审阅。**
更小机器使用不带 `--detect` 的 `host init`，手动修改容量。

已有应用配置时，保留原来的模板、NUMA 和容量：

```bash
bash scripts/clawbox experiment host init "$HOME/.config/clawbox/host.yaml" \
  --from-profile "$HOME/.config/clawbox/host.json"
${EDITOR:-vi} "$HOME/.config/clawbox/host.yaml"
```

检查 `runtime_template`、`tool_template`、`node`、`cube_source` 和目录。
`node` 是 CubeSandbox 注册名，不是 NUMA 编号；模板须在该节点 READY。

| 主机参数 | 单位与约束 |
| --- | --- |
| `compute_nodes[].numa_node`, `cpus` | NUMA 编号及其 CPU 列表字符串，例如 `"0-39"`；null 使用该节点全部 CPU，不同域不能重叠 |
| `memory_gib`, `low_gib`, `high_gib` | 每域 GiB 容量和水位：`0 < low < high < memory` |
| `warm`, `warm_node`, `warm_gib` | 共享池开关、独立 NUMA 编号、GiB 容量 |
| `shared_borrow_percent` | 运行 VM 可借用共享池的最大百分比，0–50 |
| `warm_root` | 专用 tmpfs 路径，不覆盖其他数据目录 |
| `cold_root`, `cubelet_data_root`, `output_root` | 冷快照、现有 Cubelet 数据、结果路径；改字段不会迁移已有数据 |
| `minimum_disk_free_gib` | 默认 20 GiB 最低剩余空间；磁盘使用率同时须低于 85% |

machine.env 中的 `CLAWBOX_OUTPUT_ROOT`、`CLAWBOX_WARM_ROOT`、`CLAWBOX_COLD_ROOT`
应与 host YAML 对应路径一致。完整说明见[主机配置](supernode.md)。

## 磁盘维护

```bash
df -hT /data /data/cubelet
bash scripts/clawbox --sudo experiment host clean
bash scripts/clawbox --sudo experiment host clean --apply
df -hT /data /data/cubelet
```

`clean` 只预览，`--apply` 才删除。默认扫描 `/var/crash`、`/var/lib/systemd/coredump`，
只选至少 24 小时未修改、至少 100 MiB、名称和文件头符合规则的崩溃转储，显示实际分配
字节数。保留小型诊断日志，不跟随符号链接，不删硬链接、镜像、模板、实验结果或任意大文件。
先保留仍需分析的转储。空间释放可能稍后才反映到 `df`。

清理当天已完成的转储，可缩短保留时间；先预览同一组选项：

```bash
bash scripts/clawbox --sudo experiment host clean --older-than-hours 1 --min-size-mib 100
bash scripts/clawbox --sudo experiment host clean --older-than-hours 1 --min-size-mib 100 --apply
```

`--root /dedicated/crash/path` 可重复指定，替换默认目录；不跨文件系统。
仍不足时用 `sudo du -xhd1 /var /data` 定位占用并扩容或迁移对应存储。
不要靠删除 Cubelet 数据或 `docker system prune --volumes` 腾空间。

## 检查并应用主机配置

首次配置、修改 host YAML 或重启后，在 VM 池空闲时执行：

```bash
sudo -v
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox experiment host apply "$HOME/.config/clawbox/host.yaml"
```

check 不修改主机，成功为 `ready_for_apply: true`，失败逐项给出 remedy。apply 重新检查，
设置 cgroup/存储/必要服务，并创建、执行、删除探测 VM，成功后才保存 host.json。
失败后修复原因重试。改变现有 tmpfs 的 NUMA 时，先保留快照，在空闲时卸载旧挂载或换新空
目录。服务和 S3lvol 恢复见[安装指南](installation.md#after-a-reboot)。

## 最小实验

```bash
mkdir -p "$HOME/clawbox-specs"
bash scripts/clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/smoke.yaml" --trace "$PWD/examples/traces/smoke.jsonl" \
  --experiment-id first-smoke --concurrency 2
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/smoke.yaml" --inputs
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/smoke.yaml"
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/smoke.yaml" --run-id smoke-01
bash scripts/clawbox experiment status smoke-01
bash scripts/clawbox --sudo experiment report smoke-01
```

configure 读取已应用的主机配置中的模板、镜像摘要、NUMA 和容量，不会调整 VM 模板大小。
单计算域也可运行并发 2，资源不足改为 1。输出 YAML 已存在时先检查内容，确定覆盖才加
`--force`。更改配置后用新 run ID。

成功标准是最终 `state: succeeded`、任务验证成功、清理已验证；starting 或服务 active
不是完成。任务通过两个 VM 将 complete 写入 `/workspace/result.txt`，只证明执行链路。
launch 自动检查输入、主机和真实预检，然后运行负载。
启用共享借用时，预检会在专用 Tool VM 的 `/dev/shm` 写入至多 128 MiB，随后销毁该 VM；
分配上限为 Tool 内存的 1/16 与 128 MiB 中的较小值，并保留 4 MiB tmpfs 空间。
镜像须提供 `dd`，tmpfs 至少有 5 MiB 可用空间；结果记录实际分配量。
安装主机 numactl 包可额外保存 `numastat -p`，
未安装时明确标记缺失；cgroup 驻留量和 `/proc/PID/numa_maps` 仍会保存。该探测不会改写正式 trace。
结果在 `CLAWBOX_OUTPUT_ROOT/RUN_ID`。临时换目录时所有相关命令都加
`bash scripts/clawbox --output-root /absolute/results experiment ...`。

## 训练与策略对比

短 smoke 不保证采到内存峰值；下面的训练任务触碰 64 MiB 并保持三秒：

```bash
bash scripts/clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/memory-train.yaml" --trace "$PWD/examples/traces/memory-smoke.jsonl" \
  --prompt 'Allocate and touch 64 MiB for three seconds, then write complete to /workspace/result.txt.' \
  --repository self-service/memory --experiment-id memory-train --baseline default-resident --concurrency 2
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/memory-train.yaml" --run-id memory-train-01
```

train 的输入是实际结果目录；RESULTS 填写 machine.env 中的结果根目录：

```bash
RESULTS=/data/clawbox-results
bash scripts/clawbox experiment train "$RESULTS/memory-train-01" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" --repository self-service/memory \
  --output "$HOME/clawbox-specs/memory-p50.json"
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/memory-train.yaml" \
  "$HOME/clawbox-specs/control-eval.yaml" --experiment-id control-eval \
  --suite admission \
  --prediction-artifact "$HOME/clawbox-specs/memory-p50.json" \
  --concurrency 2 --repetitions 1
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/control-eval.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/control-eval.yaml" --run-id control-eval-01
bash scripts/clawbox --sudo experiment report control-eval-01
```

训练接受成功完成的无准入或固定准入运行，要求完整观测、任务验证和清理证明；缺失观测不能填零。
准入套件默认比较无准入部署与 ClawTune 预测准入，两组使用 OS 放置和常驻 VM。
默认关闭 AgentCgroup；`--runtime-control agentcgroup_cgroup` 为方案组开启它，
`--runtime-control off` 关闭。加 `--ablations` 后另有固定准入和仅 AgentCgroup 两组。
固定准入默认读取训练产物的实测 Host 增量 P90，也可用 `--static-tool-memory-mib` 调整。
运行时预测需要同时保留 train 生成的 `memory-p50.json` 和 `memory-p50.kb.json`。
`--prediction-mode runtime` 每次查询实际命令，包括预计算列表中没有的命令；
`--prediction-mode frozen` 使用冻结的逐命令预测。KB 在评估期间不学习测试样本。
可用 `--prediction-reservation-scale 0.8 --admission-margin-mib 128` 减少保守预留；
这里是调参示例，不是推荐最优值。默认分别为 1 和 256 MiB，应同时检查排队、完成率和内存压力。
这些变更使用新 run ID，无需重建镜像或重新 apply 主机；更换预测环境需要重新训练。
一次重复只验证流程，正式研究换真实负载并与校准数据分开，例如
`--concurrency 2,4,8 --repetitions 8`，保留失败样本和完成率。

NUMA/快照使用独立套件。先在 host YAML 配置 compute_nodes、WARM 与 COLD 路径，
执行 check/apply。准备包含真实任务模板和 trace 的 BASE YAML；每个 case 填写有依据的
`snapshot_reuse_probability`，resources 填写实测 `snapshot_residency_costs`
（warm_restore_seconds_per_gib、cold_restore_seconds_per_gib、provenance）。
这些工作负载参数不能从主机物理容量推断。可参考
`examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml` 的格式；
其中模板身份和测量来源仅属于原主机，不能直接复用到新主机。

```bash
BASE="$HOME/clawbox-specs/numa-workload.yaml"
bash scripts/clawbox experiment configure "$BASE" \
  "$HOME/clawbox-specs/snapshot-eval.yaml" --experiment-id snapshot-eval \
  --suite numa-snapshot --concurrency 2 --repetitions 1
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/snapshot-eval.yaml" --inputs
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/snapshot-eval.yaml"
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/snapshot-eval.yaml" --run-id snapshot-eval-01
bash scripts/clawbox --sudo experiment report snapshot-eval-01
```

BASE 还须提供独立来源的模型等待预测及其来源。NUMA 套件不需要 ClawTune 预测文件：
主基线为 OS 默认放置、VM 常驻、无 Tool 准入和 AgentCgroup；方案组使用负载感知放置、
安全空闲回收、模板亲和与快照价值排序。主机允许共享借用时方案组使用先回收后借用规则。
加 `--ablations` 后增加 load-aware-lru、template-aware-only、snapshot-aware-only，合计五组。
主比较衡量整个管理方案；三个受控对照用于拆分机制贡献，不能把 LRU 对照称为默认部署。
`--runtime-control` 可独立控制方案和受控对照的 AgentCgroup，主基线始终关闭。
OS 基线继承相同宿主 cgroup 边界，使用 LOCAL 加 live-borrow 额度对应的共同硬限制；不会把全部 WARM 容量算成额外可用内存。
物理共享验证由套件自动要求。
实测 COLD 不慢于 WARM 时会提示保留收益为零；保留该负结果。
轻负载可能不触及回收水位；预检执行快照和恢复，不等于正式负载有性能收益。

生成的套件 YAML 省略默认值和重复策略列表；`describe` 展示生效参数，运行目录保存
完整配置。修改实验规模或套件需使用新 run ID；修改主机预算、拓扑或存储需重新 apply。
不改变模板或预测测量环境时无需重建镜像或重新训练。

## 修改参数或工作负载

```bash
bash scripts/clawbox experiment baselines
bash scripts/clawbox experiment configure --help
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/memory-train.yaml" \
  "$HOME/clawbox-specs/custom.yaml" --experiment-id custom \
  --concurrency 1,2 --repetitions 3 --command-timeout-seconds 120 --arm-timeout-seconds 600
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/custom.yaml" --inputs
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/custom.yaml"
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/custom.yaml" --run-id custom-01
```

| 修改内容 | 方法 |
| --- | --- |
| 负载 | `--trace FILE --prompt TEXT --repository ID --base-commit COMMIT --validation-command COMMAND`；多任务编辑 workload.cases |
| 并发/重复/随机种子 | `--concurrency 1,2,4 --repetitions 3 --random-seed 0` |
| admission 总预算/安全余量 | `--pool-memory-gib`、`--checkpoint-headroom-gib`、`--emergency-free-memory-gib`，单位 GiB |
| 快照实现/存储 | `--snapshot-mechanism full-copy` 或 incremental-cow；`--snapshot-storage warm-only` 或 tiered |
| 采样/等待/超时 | 采样间隔编辑 YAML 的 execution.memory_sample_interval_seconds；等待/超时用 `--time-scale`、`--command-timeout-seconds`、`--arm-timeout-seconds` |
| VM CPU/RAM/可写盘 | [注册新模板](installation.md#4-register-templates-and-verify-ssh)，更新 host YAML、apply、configure |
| NUMA/CPU 集合/本地容量/共享池 | 改 host YAML，check、apply 后重新 configure |
| ClawTune/镜像 | 用相同版本更新主机与两个镜像，apply、configure；新运行重新训练 |

无专用 CLI 开关的字段编辑生成的 YAML；默认值和含义见[配置参考](guide.md)。更换 trace
不会安装 guest 依赖；工具镜像须包含任务仓库、起始版本和依赖。完整 launch 路径使用
OpenClaw + replay。配置容量、预测预留、实际内存和快照字节数分别解释。

### 比较 NUMA 放置策略

先复制已有 host YAML，在副本中设置 `warm: false`，保留相同的 compute nodes、
CPU 集合和本地内存预算。不要在有运行中的 VM 时切换主机配置。下面假设副本为
`$HOME/.config/clawbox/host-local.yaml`；原配置保留用于之后恢复共享池。

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host-local.yaml"
bash scripts/clawbox experiment host apply "$HOME/.config/clawbox/host-local.yaml" \
  --profile "$HOME/.config/clawbox/host-local.json"
for placement in os_default round_robin load_aware; do
  bash scripts/clawbox experiment configure "$HOME/clawbox-specs/smoke.yaml" \
    "$HOME/clawbox-specs/$placement.yaml" \
    --profile "$HOME/.config/clawbox/host-local.json" \
    --experiment-id "$placement" --placement-policy "$placement" \
    --baseline fixed-resident --concurrency 2 --repetitions 3
  bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/$placement.yaml" \
    --run-id "$placement-01"
  bash scripts/clawbox --sudo experiment report "$placement-01"
done
```

`os_default` 不缩窄单个 VM 的 NUMA 绑定；`round_robin` 按顺序分配计算节点；
`load_aware` 在满足 CPU 和内存准入条件的节点中选择压力较低者。后二者将同一
会话的 Runtime 和 Tool 放在同一计算节点，恢复时保持原节点。YAML 中
`execution.placement_cpu_overcommit` 默认 8，单位为每个计算 CPU 可准入的
vCPU 数；它不是实际利用率。调整策略或该比值后使用新 run ID，无需重建镜像。
比较正式任务时替换为已验证的真实 trace、仓库和任务环境；smoke 只验证机制。

### 比较共享池调度

先在没有运行中 VM 时恢复原共享池配置，并生成基准配置：

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/smoke.yaml" \
  "$HOME/clawbox-specs/pool-base.yaml" --baseline fixed-resident \
  --placement-policy round_robin --concurrency 16,32,64,128,256 --repetitions 3 \
  --model-wait-prediction-seconds auto
```

复制 `pool-base.yaml` 为四份，保持 trace、模板、种子、到达模式及资源预算一致。
仅按下表编辑 `resources.memory_scheduling` 和 `policies`：

| 比较项 | memory_scheduling | reclamation | eviction | restore |
| --- | --- | --- | --- | --- |
| Local-only | `local_only` | `resident` | `none` | `none` |
| Static Borrowing | `static` | `resident` | `none` | `none` |
| Snapshot-only | `snapshot_only` | `snapshot_pause` | `wait_aware_pressure` | `reactive` |
| Joint Scheduling | `joint` | `snapshot_pause` | `wait_aware_pressure` | `reactive` |

`policies[].name` 分别设为 `local-only`、`static-borrowing`、`snapshot-only`、`joint`；
`admission: fixed` 和 `runtime_control` 保持一致。快照策略使用原有空闲/模型等待
生命周期，活跃 Tool 不能作为回收对象。配置字段与限制见[调度参考](guide.md#numa-memory-scheduling)。
`auto` 从输入 replay trace 计算固定的模型等待估计并保存来源；它不是独立预测模型，
不能用于宣称等待预测的泛化能力。自备预测时同时填写估计秒数和来源。

Snapshot-only 不依赖 live borrowing 开关：保留 WARM 容量和目录即可使用共享池快照，
NUMA HIGH/LOW 控制也会独立运行。正 WARM 容量必须配置 `warm_snapshot_root`，
`tiered` 还必须配置 `cold_snapshot_root`，以便容量不足时回退。上述四组比较应保留
相同主机配置，仅由调度模式禁止借用；若更改主机借用额度，仍需重新 `host apply`。
修改策略后使用新 run ID 并重新预检，无需重建镜像或重新训练。

Joint 默认在安全空闲 VM 中先尝试 WARM 回收，再尝试 live borrowing。完成一轮
独立校准后，可以在 Joint YAML 中加入下面的可选配置，让控制器按候选 VM 的配置
容量比较实测动作代价：

```yaml
resources:
  return_cooldown_seconds: 5
  memory_action_costs:
    provenance: <CALIBRATION_ARTIFACT_OR_EXPERIMENT_ID>
    live_borrow_service_seconds_per_gib: <MEASURED_POSITIVE_VALUE>
    live_borrow_runtime_penalty_seconds_per_gib: <MEASURED_NONNEGATIVE_VALUE>
    warm_checkpoint_seconds_per_gib: <MEASURED_POSITIVE_VALUE>
    warm_restore_seconds_per_gib: <MEASURED_POSITIVE_VALUE>
    cold_checkpoint_seconds_per_gib: <MEASURED_POSITIVE_VALUE>
    cold_restore_seconds_per_gib: <MEASURED_POSITIVE_VALUE>
    expected_restore_probability: <VALUE_FROM_0_TO_1>
    remote_access_latency_ns: <OPTIONAL_MEASURED_POSITIVE_VALUE>
    remote_stream_bandwidth_mib_per_second: <OPTIONAL_MEASURED_POSITIVE_VALUE>
```

尖括号是必须替换的占位符，不能直接运行；没有延迟或带宽测量时删除最后两个可选字段。
六项动作代价的单位都是“秒/配置 VM GiB”；使用 `tiered` 时 COLD 两项必填，
`warm-only` 可以删除它们。
`provenance` 必须指向实际校准产物或实验 ID。`expected_restore_probability` 是显式的
实验假设，不是测量出的硬件属性。原始 NUMA 带宽和延迟不能直接替代这些端到端
动作代价。删除 `memory_action_costs` 即恢复确定性回退。修改这些字段或冷却时间需要
新 run ID 和重新预检，不需要 host apply、镜像重建或 ClawTune 重训练。Joint 在
LOCAL 低于 LOW 且没有阻塞需求时尝试归还一个 VM；只有该 VM 当前实测的 shared
驻留量回迁后仍不超过 LOCAL HIGH 才执行。若仍有远端页，借用额度继续保留。

分别保存为同名 YAML 后运行：

```bash
for policy in local-only static-borrowing snapshot-only joint; do
  bash scripts/clawbox experiment validate "$HOME/clawbox-specs/$policy.yaml" --inputs
  bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/$policy.yaml" \
    --run-id "pool-$policy-01"
  bash scripts/clawbox --sudo experiment report "pool-$policy-01"
done
```

先用已验证的真实任务配置替换 smoke，检查任务仓库、起始版本、依赖与 trace 一致。
不能将 smoke、裁剪任务或未确认起始状态的 trace 当作 SWE-ReBench/TerminalBench
正式结果。先验证容量受限组，再测试更高并发；Agent 并发不是实际 Tool 并发。
对 `round_robin` 和 `load_aware` 重复同一内存策略，使用新的 run ID。每组至少三次；
报告保留失败、OOM、排队超时及清理结果，不能只比较成功样本。

## 后台运行与恢复

```bash
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/smoke.yaml" --run-id smoke-01 --detach
bash scripts/clawbox experiment status smoke-01
bash scripts/clawbox --sudo experiment report smoke-01
```

detach 在预检结束后才将正式实验转到后台。重新连接后进入项目目录，执行相同 launch：
正在运行返回状态，已成功返回报告，中断则恢复。重启先 apply。不要删除结果目录来修复运行。

停止并清理自己拥有的实验，保留结果：

```bash
bash scripts/clawbox --sudo experiment abort smoke-01
bash scripts/clawbox --sudo experiment destroy smoke-01
```

| 失败 | 处理 |
| --- | --- |
| host check 失败 | 按 remedy 修复并重跑；磁盘先用 host clean 预览 |
| VM pool busy | status 查看任务，等待或 abort/destroy 自己的 run ID |
| default AgentCgroup control was not verified | 查看结果根目录 `.qualification-probes/` 最新探测的 `runtime-control/vm-control.json`；它区分控制激活、LOW 节流和 OOM。确认主机空闲后重试；持续失败须修复控制环境，不要绕过预检 |
| 模板无 READY 副本 | 检查 ID、注册节点、注册日志，不盲目重复提交 |
| no more resource | 检查磁盘使用率和 S3lvol socket/backend |
| ClawTune revision 不匹配 | 同源更新主机和两个镜像，再 apply/configure |
| guest telemetry 缺失 | 检查 guest kernel/BCC headers，按安装指南修复镜像 |
| 预测覆盖失败 | 修复输入身份或补采训练数据，不伪造零观测 |

结果目录的 `run-state.json`、`report.md`、`summary.json`、`arms/`、`attempts/` 保存状态、
报告和日志。详见[结果参考](guide.md#reading-the-results)。

## Guest-to-host memory diagnostics

Use a separate diagnostic recipe, without `suite`, to inspect guest unmap and
host reclamation. The ordinary experiment workflow still owns all VMs:

```bash
mkdir -p "$HOME/clawbox-specs"
python3 scripts/make-memory-reclaim-trace.py "$HOME/clawbox-specs/reclaim-64.jsonl" --allocation-mib 64 --hold-seconds 3 --settle-seconds 5
bash scripts/clawbox experiment configure examples/experiments/getting-started.yaml "$HOME/clawbox-specs/reclaim.yaml" --trace "$HOME/clawbox-specs/reclaim-64.jsonl" --baseline uncontrolled-resident --concurrency 1 --repetitions 1
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/reclaim.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/reclaim.yaml" --run-id reclaim-01
bash scripts/clawbox --sudo experiment report reclaim-01
```

The generator refuses to overwrite an existing trace. Change allocation size
only within the registered Tool VM capacity, use a new trace and run ID, and
compare guest phase timestamps in `tool-artifacts/memory-reclaim-phases.json`
with host `memory_sample` charges, swap and PSI. Guest unmap alone is not proof
of host reclamation.

For a separate guarded OS diagnostic, use a local-only applied host profile,
resident policies, and these YAML resource settings:

```yaml
lifecycle_admission: os-managed
proactive_cache_reclaim: false
memory_scheduling: local_only
shared_memory_borrow_limit_mib: null
```

Run `validate --inputs` and `describe`, then use a new run ID. Apply exactly the
same host limits, CPU and memory masks to all diagnostic arms. OS-managed mode
keeps hard limits and startup safety checks; it does not promise no OOM or
change swap policy. Changing host limits or swap requires separate host setup
and verification. These diagnostics do not replace either suite's baseline.
