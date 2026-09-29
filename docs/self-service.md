# 从主机配置到第一次实验

在 ARM64 Linux/KVM 主机的终端中执行下面的命令。`clawbox experiment`
是实验入口；不需要 Codex 或其他代码代理。先完成单并发例子，再增加并发和策略。

## 1. 新机器准备

先按[安装指南](installation.md)的第 1–4 步安装主机依赖、带补丁的 CubeSandbox、
匹配的 Python SDK，导入 Guest 镜像和内核并注册 Runtime/Tool 模板。每一步的检查
通过后再继续。仓库没有发布完整 Guest 镜像与内核；需要从已有部署导出，或提供同等
构件。仅 `git clone` 和 `pip install` 不足以创建可运行的 VM。

磁盘先由管理员挂载到预定目录。安装 CubeSandbox 前，在其安装 `.env` 中选择实际
存储位置；这里的主机配置不会格式化磁盘、移动已有 Cubelet 数据或修改 S3 凭据。
先确认文件系统，再分配容量：

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
df -hT /data /data/cubelet
numactl --hardware
```

以下命令从 ClawBox checkout 执行。每次新登录先加载环境：

```bash
cd "$HOME/src/ClawBox"  # 改为你的 checkout
source .venv/bin/activate
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
sudo -v
clawbox experiment host inspect
```

`inspect` 显示 NUMA 编号、CPU 列表、物理容量和距离。`free_mib` 不包含可回收缓存，
不能把它当作整机可用内存，也不能把物理总容量全部承诺给实验。

## 2. 保存并检查主机配置

```bash
clawbox experiment host init "$HOME/.config/clawbox/host.yaml"
${EDITOR:-vi} "$HOME/.config/clawbox/host.yaml"
```

已有成功的 `host.json` 时，可改用以下命令生成初始配置；目标文件必须不存在：

```bash
clawbox experiment host init "$HOME/.config/clawbox/host.yaml" \
  --from-profile "$HOME/.config/clawbox/host.json"
```

至少填写 `runtime_template`、`tool_template`、`node`、`cube_source`，检查所有目录。
`node` 是两个模板均 READY 的 CubeSandbox 节点名称/IP，不是 NUMA 编号。
旧 profile 没有保存 COLD 路径时，必须把 `cold_root` 改成实际安装值。

| 主机 YAML 字段 | 用途 |
| --- | --- |
| `local_node` | 本地 CPU 与内存所在 NUMA；CPU 取该节点 CPU 集合 |
| `local_gib` | LOCAL 层容量边界，GiB |
| `low_gib`, `high_gib` | 回收目标与触发水位，满足 `0 < LOW < HIGH < LOCAL` |
| `warm` | 是否配置共享内存与 WARM 快照；只运行常驻方案可设为 `false` |
| `warm_node`, `warm_gib` | 共享池 NUMA 与容量，须与 LOCAL 不同节点 |
| `shared_borrow_percent` | 活跃 VM 借用共享池的最大比例，0–50；其余容量与快照共享计账 |
| `warm_root` | 独立 tmpfs 挂载目录；不能隐藏已有磁盘文件，也不能与其他数据目录嵌套 |
| `cold_root` | 磁盘快照目录；WARM-only 实验不向它溢出 |
| `cubelet_data_root` | **检查** Cubelet 实际数据盘空间的路径，不改变 Cubelet 的存储配置 |
| `output_root` | **检查**结果盘空间；启动时仍显式传 `--output-root` |
| `minimum_disk_free_gib` | 每个数据文件系统需要的最低空闲空间；还须保持使用率低于 85% |

```bash
clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
clawbox experiment host apply "$HOME/.config/clawbox/host.yaml"
```

`check` 只读，不启动服务、不建 VM、不修改限制；退出 0 且
`ready_for_apply: true` 表示可以应用配置。失败退出 2，每项提供 `detail` 和 `remedy`。
YAML 格式或字段错误退出 1。它检查安装前提，不代表实验已通过。

`apply` 再检查一次，在空闲 VM 池上配置 cgroup、WARM、服务参数；参数改变时启动或
重启相应 CubeSandbox 服务。随后创建探测 VM、执行命令并销毁，全部通过才保存
`~/.config/clawbox/host.json`。失败不会写成功 profile；部分主机设置可能已经应用，
修复报告中的原因后重跑同一条命令。不会自动清空磁盘或销毁其他实验 VM。
Cubelet 重启后可能需要数分钟重建网络并上报心跳；脚本最多等待十分钟，期间输出进度。
systemd 的 `active` 不表示节点已经可以创建 VM。

修改 NUMA、容量、水位或路径后重复 `check`、`apply`，再重新生成实验 YAML。
切换已挂载 WARM 的 NUMA 时，先在空闲池上保存所需快照并由管理员卸载旧挂载，
或者选择新的空目录。重启主机后也要重新 `apply`，恢复 tmpfs 和 cgroup。

这套模拟用一台机器的 NUMA 域表示 LOCAL 与共享内存，约束 CPU 位置、内存层和借用。
它没有模拟任意超节点的交换网络、跨机协议或可配置互连带宽/延迟。NUMA 距离由真实
硬件决定。`local_gib + warm_gib × 借用比例` 是活跃 VM 父 cgroup 的组合上限；
LOCAL 边界由实验控制器依据 NUMA 测量实施，不能把组合上限误读为 LOCAL 占用。

## 3. 运行最小例子

选可写的结果目录，首次使用先创建：

```bash
export CLAWBOX_OUTPUT_ROOT="$HOME/clawbox-results"
mkdir -p "$CLAWBOX_OUTPUT_ROOT" "$HOME/clawbox-specs"
clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/smoke.yaml" \
  --trace "$PWD/examples/traces/smoke.jsonl" \
  --experiment-id first-smoke --concurrency 1 --pool-memory-gib 32
clawbox experiment validate "$HOME/clawbox-specs/smoke.yaml" --inputs
clawbox experiment describe "$HOME/clawbox-specs/smoke.yaml"
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/smoke.yaml" --run-id smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment status smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment report smoke-01
```

这里的 32 GiB 准入预算对应默认主机 HIGH=32；若改过主机容量，一并修改。
`configure` 从成功 profile 填入模板、镜像摘要、NUMA 和容量；默认拒绝覆盖已有 YAML。
覆盖时显式加 `--force`，并为新实验选择新 run ID。

`launch` 自动检查环境、验证目标并发的真实 VM 运行，再启动正式实验。
成功标准是最终 `state: succeeded`、结果验证和清理通过，而不是只看到服务 active
或探测 VM 创建成功。例子会通过 Runtime 调用 Tool，在 `/workspace/result.txt`
写入 `complete` 并验证。它验证执行链路，不代表已验证高并发内存压力实验。

同一 run ID 再次 `launch` 会显示/恢复原任务；不会把成功任务重复运行。
长实验可以加 `--detach`，然后用 `status` 查看。主动停止和清理：

```bash
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment abort smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment destroy smoke-01
```

## 4. 改工作负载、资源与策略

先用下面的小内存负载走完训练和三方案评估。它启动 Python，分配 64 MiB 并保持三秒，
让 Guest 内存采样有可观测窗口。普通 `smoke.jsonl` 的内建 `printf` 太短，不适合作为
内存训练集；`No valid Cube guest extra-memory labels` 不能通过填零绕过。

```bash
clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/memory-train.yaml" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" \
  --prompt 'Allocate and touch 64 MiB for three seconds, then write complete to /workspace/result.txt.' \
  --repository self-service/memory --experiment-id memory-train \
  --baseline tool-static-resident --concurrency 1 --pool-memory-gib 32
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/memory-train.yaml" --run-id memory-train-01
clawbox experiment train "$CLAWBOX_OUTPUT_ROOT/memory-train-01" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" --repository self-service/memory \
  --output "$HOME/clawbox-specs/memory-p50.json"
clawbox experiment configure "$HOME/clawbox-specs/memory-train.yaml" \
  "$HOME/clawbox-specs/memory-eval.yaml" --experiment-id memory-eval \
  --baseline tool-static-resident --baseline tool-p50-resident \
  --baseline tool-p50-wait-reactive \
  --prediction-artifact "$HOME/clawbox-specs/memory-p50.json" \
  --static-tool-memory-mib auto --model-wait-prediction-seconds auto \
  --snapshot-storage warm-only
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/memory-eval.yaml" --run-id memory-eval-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment report memory-eval-01
```

此步骤要求 `warm: true`。单样本只验证训练/预测的数据链路，不用于性能结论。
轻负载未触发 HIGH 时正式 arm 可以没有暂停；qualification 仍须验证配置的快照恢复
与共享借用。确认成功后，用自己的真实任务、独立训练与多次重复开展正式实验。

使用[训练与评估流程](lab.md#2-用命令生成实验配置)，先运行 A 的真实训练，再用
`experiment train` 导出预测文件，最后运行 A / A+B / A+B+C。训练输入必须是完整、
成功、清理已验证的独立训练 run；不能用 qualification 前缀或伪造内存数据代替。
新任务的 Tool 模板须包含对应仓库、依赖和正确基线版本；修改 trace 不会自动安装任务环境。

```bash
clawbox experiment configure --help
clawbox experiment baselines
```

| 想修改什么 | 操作位置 |
| --- | --- |
| 并发、重复次数 | `configure --concurrency 1,4,8 --repetitions 3` |
| 准入预算、恢复余量、整机安全余量 | `--pool-memory-gib`、`--checkpoint-headroom-gib`、`--emergency-free-memory-gib` |
| 本地内存与共享池物理配置 | 改主机 YAML 后 `host apply`，再 `configure` |
| 每个 VM 的 vCPU、Guest RAM | 用安装指南的模板注册命令更改 `--cpu-millicores`、`--memory-mib`，将新模板 ID 写回主机 YAML 并 `apply` |
| 每个 VM 的可写盘 | 注册模板时 `--writable-layer-size 40G`；不会因改变结果目录而改变 |
| Cubelet 数据盘 | 安装时设置实际存储位置；已有数据迁移由管理员完成，再更新 `cubelet_data_root` 检查路径 |
| 快照方式 | `--snapshot-mechanism full-copy` 或 `incremental-cow` |
| 快照层 | `--snapshot-storage warm-only` 或 `tiered`，按所选策略支持范围使用；`--cold-root` 必须与主机服务一致 |
| 命令/整轮超时、采样间隔、随机种子 | `--command-timeout-seconds`、`--arm-timeout-seconds`、`--memory-sample-interval-seconds`、`--random-seed` |
| P50 预测、静态内存预留、模型等待预测 | `--prediction-artifact`、`--static-tool-memory-mib`、`--model-wait-prediction-seconds` |

未提供 CLI 快捷参数的配置可以编辑生成的 YAML；完整字段见[用户指南](guide.md)。
每次改动后运行 `validate --inputs`、`describe` 和 `launch`，重新执行必要资格验证。
VM 配置 RAM、准入预留、实测 LOCAL/共享内存、WARM 快照空间是不同量，报告中应分别读取。

## 5. 遇到失败时

| 输出/现象 | 可执行的下一步 |
| --- | --- |
| `host check` 失败 | 按对应 `remedy` 修正配置或安装，再重跑 check；不要先 apply |
| VM 池不空闲 | `experiment status RUN_ID`，等待完成或对自己 run 执行 abort/destroy |
| 模板无 READY 副本 | 检查节点名称和模板注册输出；不要把 NUMA 编号当节点名 |
| `no more resource` / snapshot storage unavailable | 检查 Cubelet 数据盘 `df -h`，低于 85% 使用率并留出快照空间；检查 S3lvol socket 与后端，见安装指南 |
| ClawTune revision 不匹配 | 按用户指南运行 `experiment images` 更新 Runtime 和 Tool，重新 configure；不要跳过验证 |
| WARM policy 不匹配 | 确认配置节点和 `findmnt -M WARM_PATH`；换新空目录或在保存快照后调整旧挂载 |
| Guest BCC/遥测失败 | 按安装指南核对运行 Guest 内核与 headers，修复镜像后重新注册模板 |
| SSH 断开/实验中断 | 同一 YAML、run ID 再次 launch；保留旧结果，勿手工删除 ownership 文件 |

失败日志、`run-state.json` 和每个 arm 的原始结果留在结果目录，可用于定位。
不需要为了重试删除整个结果目录，也不要把失败退出码当作可以继续下一步的提示。
