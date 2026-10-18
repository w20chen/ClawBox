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

空闲回收使用真实未完成的模型请求和内存压力，不配置 LLM 时长预测。
`idle-reclaim` 配方生成 resident、wait-tool、wait-pair 三组；
`mechanisms.shared_memory.borrow: off` 将借用排除在候选选择之外。
这些实验设置修改后使用新 run ID，无需 host apply、重建镜像或重训。

Agent 发生 OOM、命令超时或普通异常时，实验会保留其失败记录并继续其他 Agent。
查看 report 中的完成数与全部提交数，以及 `correctness.session_failures`；不要只统计成功样本。
该臂完成且清理验证通过后，后续比较臂会继续运行。
共享控制器或资源清理失败、整臂截止时间到期和显式 abort 才会中止整臂。
整臂截止后，supervisor 最多等待 YAML 中 `execution.arm_finalization_timeout_seconds`
秒（默认 300）完成清理并写出部分结果；这段时间不供 Agent 继续工作。
失败隔离代码更新后用新 run ID；已有运行不会热更新，也不需要重建镜像或重训。

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

大规模并发时，可在 `machine.env` 设置 `CLAWBOX_NOFILE_LIMIT=65536`。
单位是单个 ClawBox 进程允许同时打开的文件描述符数量；未设置时保持主机当前软上限。
该值不能超过 `ulimit -Hn`，wrapper 会在启动前校验并给出修复提示。修改后只需重新执行
`scripts/clawbox`，不需要 host apply、镜像重建或重训。

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
| `tcp_tw_reuse` | 默认 `null`，不修改网络设置；`0` 禁用 TIME_WAIT 复用，`1` 全局启用，`2` 仅 loopback。作用于宿主网络命名空间，需 host apply；重启后再次 apply，无需重建镜像或重训 |
| `cubelet_tap_init_num` | 默认 `null`，保留 Cubelet 当前设置；填写启动时预创建的 TAP 数量。至少覆盖并存的 Runtime VM 与 Tool VM 总数（例如 c640 最坏为 1280，可设 1536）。修改需在空闲主机执行 host apply，会重启 Cubelet/egress；无需重建镜像或重训 |

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

高并发时若已确认短连接耗尽客户端端口（例如 MinIO 连接报 `Cannot assign requested address`），
可在 host YAML 显式保存 `tcp_tw_reuse: 1`，先 `host check` 查看当前值和请求值，再 `host apply`。
它允许协议安全条件下的 TIME_WAIT 复用，不保证消除所有连接瓶颈；取值含义见
[Linux 网络参数说明](https://docs.kernel.org/networking/ip-sysctl.html#tcp-tw-reuse-integer)。
所有对比臂使用相同设置。恢复时填写此前的明确取值并 apply；`null` 表示保持当前值，不表示恢复默认。
该设置保存在 host.json，`host init --from-profile` 会保留它，不再需要每次手动运行 sysctl。
`cubelet_tap_init_num` 同样保存在 host.json。Cubelet 会逐个预建 TAP；大池首次启动可能需要数分钟，host apply 随后的真实 VM 探测成功后才表示主机可用于实验。

## 最小实验

已有经过审阅的 host YAML 和实验 YAML 时，可直接使用同一个 launch 入口：

```bash
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/smoke.yaml" \
  --apply-host "$HOME/.config/clawbox/host.yaml" --run-id smoke-01 --qualification-concurrency 2
```

`--apply-host` 在输入校验后检查并应用主机配置，创建/执行/删除探测 VM，通过后把主机 profile
保存为 host YAML 同目录同名的 `.json`，再进行实验预检和运行。适用于重启或主机配置变更后，
要求 VM 主机空闲；已有环境未变化时省略此选项即可。首次填写配置仍先用 `host check` 审阅。
同一 run ID 正在运行或已成功时，重复命令只返回状态/报告，不重复 apply 或运行。
中断任务需要保留主机现状恢复时省略 `--apply-host`；若需重新 apply，先 abort/destroy 该 run ID。

首次创建实验文件使用以下命令：

```bash
mkdir -p "$HOME/clawbox-specs"
bash scripts/clawbox experiment configure examples/experiments/six-dimension.yaml \
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

大规模实验可在 `launch` 加 `--qualification-concurrency 2`，将前置真实 VM smoke 限为两会话（至少覆盖每个 workload case）；默认仍使用目标最大并发。receipt 分别记录实际 smoke 并发和目标并发，并保留完整输入、代码、快照/借用/放置探测及清理检查。小规模 qualification 只证明对应能力；密度和性能结论必须来自后续完整实验的实际执行。此选项无需 host apply、镜像重建或重训；更改实验参数使用新 run ID。

完整系统的回收/借用消融使用 `comparison.recipe: custom`，在对应 arm 的 `override` 只设置 `idle_reclaim.policy: off` 或 `shared_memory.borrow: off`。可保留公共 `coordination.action_selection: measured_cost` 和实测成本；仅一种能力可用时没有成本选择。两种能力都关闭仍报错；物理额度保持公共配置，修改实验臂后使用新 run ID。
比较固定/预测准入时，在公共 `mechanisms.admission.non_command_tool_memory_mib` 填写文件接口和后端维护操作的固定 MiB 预算。`prediction_reservation_scale` 只缩放模型预测的命令额度，两臂的非命令预算保持一致；调整额度仅需新 run ID，不需要 host apply、镜像重建或重训。

launch 自动检查输入、主机和真实预检，然后运行负载。
启用共享借用时，预检会在专用 Tool VM 的 `/dev/shm` 写入至多 128 MiB，随后销毁该 VM；
分配上限为 Tool 内存的 1/16 与 128 MiB 中的较小值，并保留 4 MiB tmpfs 空间。
原生 OpenClaw 的 Toolbridge 需要访问宿主 PolicyControl（默认 TCP 18080）以报告执行 start/end。即使 `allow_internet_access: false`，工作流也自动允许 `CLAWBOX_CONTROL_HOST` 这一宿主 IP；其余出站仍按配置限制。修改主机防火墙后用新 run ID 验证；此路由修复无需重建已有 progress-enabled 镜像或重训。
镜像须提供 `dd`，tmpfs 至少有 5 MiB 可用空间；结果记录实际分配量。
安装主机 numactl 包可额外保存 `numastat -p`，
未安装时明确标记缺失；cgroup 驻留量和 `/proc/PID/numa_maps` 仍会保存。该探测不会改写正式 trace。
结果在 `CLAWBOX_OUTPUT_ROOT/RUN_ID`。临时换目录时所有相关命令都加
`bash scripts/clawbox --output-root /absolute/results experiment ...`。

## 训练与六维策略对比

先运行 `bash scripts/clawbox experiment recipes` 查看实验配方。短 smoke 只验证链路。
下面的独立训练任务触碰 64 MiB 并保持三秒：

```bash
bash scripts/clawbox experiment configure examples/experiments/six-dimension.yaml \
  "$HOME/clawbox-specs/memory-train.yaml" --trace "$PWD/examples/traces/memory-smoke.jsonl" \
  --prompt 'Allocate and touch 64 MiB for three seconds, then write complete to /workspace/result.txt.' \
  --repository self-service/memory --experiment-id memory-train --concurrency 2
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/memory-train.yaml" --run-id memory-train-01
```

RESULTS 填写 machine.env 中保存的结果根目录：

```bash
RESULTS=/data/clawbox-results
bash scripts/clawbox experiment train "$RESULTS/memory-train-01" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" --repository self-service/memory \
  --output "$HOME/clawbox-specs/memory-p50.json"
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/memory-train.yaml" \
  "$HOME/clawbox-specs/admission.yaml" --recipe admission \
  --prediction-artifact "$HOME/clawbox-specs/memory-p50.json" \
  --concurrency 2 --repetitions 1
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/admission.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/admission.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/admission.yaml" --run-id admission-01
bash scripts/clawbox --sudo experiment report admission-01
```

生成无工具准入、固定额度、预测准入三组，其余五维保持公共配置。固定额度默认取独立训练
产物的 host 增长 P90。保留同时生成的 JSON 和 `.kb.json`；评估不更新 KB。正式研究替换真实任务，
训练与评估数据分开，并保留失败样本、完成率与多次重复。

做固定消融时，在单族配方后加 `--fixed-methods`。例如复用已配置和校准的 admission 文件：

```bash
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/admission.yaml" \
  "$HOME/clawbox-specs/runtime-fixed.yaml" --recipe runtime --fixed-methods
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/runtime-fixed.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/runtime-fixed.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/runtime-fixed.yaml" --run-id runtime-fixed-01
```

固定 runtime/memory-reclaim 共用固定工具准入额度；idle-reclaim 还需独立实测回收成本。
参见 [固定方法表](experiments.md#配方与受控比较)。每次更换配置使用新 run ID。
不带此选项时，其他机制仍继承公共配置。

运行时控制使用 `--recipe runtime`，放置使用 `--recipe placement`，回收使用
`--recipe idle-reclaim`，共享内存使用 `--recipe shared-memory`。
检查点使用 `--recipe checkpoint --checkpoint-axis encoding|storage|residency`，每次只研究一个子方向。
完整方案先明确公共机制，再用 `--recipe integrated`；不会自动打开全部技术。
各配方依赖、默认值、配置样例见[六维实验指南](experiments.md)。

`--recipe runtime` 生成内存保护/CPU 都关闭、仅内存保护、仅 CPU 加速、两者开启四组，并在四组中固定关闭主动内存回收。
`--recipe memory-reclaim` 单独比较主动内存回收；`--recipe shared-memory` 仅比较借用策略。
比较借用与回收的动作成本选择，使用 `--recipe coordination`；公共配置必须已启用 pressure 借用、空闲回收并填写实测动作成本。
模板亲和自动要求共享验证，不需要设置共享开关；预测实验默认统一查询 KB。
采样周期使用 `--runtime-poll-interval-seconds`，或 `--set runtime.runtime_poll_interval_seconds=0.5`，单位为秒。

生成配置后先运行 `experiment describe CONFIG`：resident 组的快照编码、存储与驻留参数会标为不生效。
CPU 释放加速要求主机提供 `compute_nodes` 或 `local_numa_node`，且不能使用 OS 默认放置。
选择 checkpoint 配方前显式设置 `--set idle_reclaim.policy=wait`；同时暂停两台 VM 再加 `--set idle_reclaim.scope=agent_and_tool`。它使用真实 LLM 等待状态，不使用时长预测或 oracle；旧配置中的预测字段必须删除。
配方与继承设置冲突时，修改对应字段后重新 configure；不要绕过 validate。配置改变使用新 run ID。

准备快照/共享池实验时，先配置并 apply 对应主机拓扑、容量与路径。按所选策略填写实测成本；
价值驻留还需每 case 复用概率。缺失证据不会填零；资格验证通过也不代表性能收益。
VM 归还 LOCAL 后若仍有共享驻留页，借用额度继续保留；再次借用复用该额度，直到共享页清空或 VM 清理完成才释放。
WARM-only 回收先预留所选范围内全部 VM 的快照空间；容量不足时保留 VM 驻留并等待或尝试允许的借用，不会只因池满使实验失败。`checkpoint_capacity_skips` 记录预留被容量限制的次数。策略修改只需重新验证并使用新 run ID，无需重建镜像或训练 Tool 内存预测器；主机容量或路径变化仍需 host apply。
查看准入等待时同时检查 admission_control 的原因计数；execution_already_inflight 表示同 VM 操作串行等待，不代表内存不足。
放置事件的 feedback_sample_age_seconds 表示内存排序观测年龄（最多约 0.2 秒）；实际准入仍检查当前用量。
创建槽只限制正在创建的 VM 对，不限制正在执行的会话；排队等待创建槽的会话不提前预留 VM 内存。

## 修改参数或工作负载

```bash
bash scripts/clawbox experiment recipes
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
| 采样/等待/超时 | 采样间隔编辑 YAML 的 measurement.memory_sample_interval_seconds；等待/超时用 `--time-scale`、`--command-timeout-seconds`、`--arm-timeout-seconds` |
| VM CPU/RAM/可写盘 | [注册新模板](installation.md#4-register-templates-and-verify-ssh)，更新 host YAML、apply、configure |
| NUMA/CPU 集合/本地容量/共享池 | 改 host YAML，check、apply 后重新 configure |
| ClawTune/镜像 | 用相同版本更新主机与两个镜像，apply、configure；新运行重新训练 |

无专用 CLI 开关的字段编辑生成的 YAML；默认值和含义见[配置参考](guide.md)。更换 trace
不会安装 guest 依赖；工具镜像须包含任务仓库、起始版本和依赖。完整 launch 路径使用
OpenClaw + replay。配置容量、预测预留、实际内存和快照字节数分别解释。

使用 `incremental-cow` 与 `tiered` 时，冷迁移会复用同一文件系统上的不可变 RAM base，
为每个快照建立独立硬链接；删除原模板路径或其他快照不会使该引用失效。跨文件系统仍需
复制 base，私有增量层仍需迁移。比较各组时保持相同的镜像和文件系统布局。
检查元数据中的 `transferred_bytes`（实际复制字节）、`reused_base_allocated_bytes`
（复用的 base 分配字节），并按 `layer_allocations` 的 device/inode 去重统计总存储。
不要把每个快照引用的共享 base 重复相加。

升级此修复时，按[安装流程](installation.md#2-build-and-install-patched-cubesandbox)
在新的源码目录构建；保留旧目录，在确认没有其他实验使用服务的维护窗口安装 Cubelet。
随后重新 qualify，并为快照比较使用新 run ID。仅此主机端修复无需重建 guest 镜像或
重新训练 ClawTune；镜像或测量语义另有变化时仍须遵守对应重建、重训要求。

### 组合或消融机制

使用 `--set dimension.field=value` 修改公共机制，或编辑 `comparison.arms[].override` 改单臂。
例如 `--set runtime.memory_protection=true` 只启用工作集保护，不开启快照或借用。
`--set idle_reclaim.policy=wait` 需要有效快照路径；在真实 LLM 等待期间按内存压力回收，不需要等待时长预测或 oracle。
设置 `--set idle_reclaim.scope=agent_and_tool` 可同时暂停 Runtime 与 Tool；Runtime 在交付模型响应前恢复，Tool 在下次执行前恢复。
此配置变更需要新的 run ID 和资格验证，不需要 host apply、镜像重建或重训。

`describe --json` 列出六维参数、各臂差异、有效资源和依赖；`plan` 展开最终 trial。
共享池账本、原始 PMU 和状态迁移证据仍由原执行链记录。运行前核对实验矩阵，避免把多个
方向同时变化的比较解释成单机制贡献。

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
bash scripts/clawbox experiment configure examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/reclaim.yaml" --trace "$HOME/clawbox-specs/reclaim-64.jsonl" --recipe custom --concurrency 1 --repetitions 1
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/reclaim.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/reclaim.yaml" --run-id reclaim-01
bash scripts/clawbox --sudo experiment report reclaim-01
```

The generator refuses to overwrite an existing trace. Change allocation size
only within the registered Tool VM capacity, use a new trace and run ID, and
compare guest phase timestamps in `tool-artifacts/memory-reclaim-phases.json`
with host `memory_sample` charges, swap and PSI. Guest unmap alone is not proof
of host reclamation.

For a separate guarded OS diagnostic, start with the schema-4 example and set:

```yaml
mechanisms:
  deployment: {mode: os-default}
  placement: {policy: os_default, template_affinity: false}
  admission: {tool: 'off'}
  runtime: {memory_protection: false, cpu_release_boost: false, proactive_memory_reclaim: false}
  idle_reclaim: {policy: 'off'}
  shared_memory: {borrow: 'off'}
```

Run `validate --inputs` and `describe`, then use a new run ID. Apply exactly the
same host limits, CPU and memory masks to all diagnostic arms. OS-managed mode
keeps hard limits and startup safety checks; it does not promise no OOM or
change swap policy. Changing host limits or swap requires separate host setup
and verification. These diagnostics do not replace either suite's baseline.
