# 六维实验配置与配方

六维表示优化方向，不代表任意组合都成立。主机容量、NUMA 汇总与借用额度先统一校验，再生成实验组；所有组共享同一物理 live 上限。
`runtime.cpu_release_boost` 要求固定 LOCAL NUMA 放置，不能与 `placement.policy=os_default` 组合。
`idle_reclaim.policy=off` 时，快照编码、存储与驻留不生效，`describe` 会提示；resident 对照组可保留这些公共参数。
只借用、不回收时不要求快照路径。选择 checkpoint 配方必须先显式开启空闲回收，避免生成没有快照操作的比较。

`checkpoint.pool_transfer_*` 同时限制快照和共享内存传输。
比较相关机制时应保持这些参数不变。配方只修改列出的字段；继承了不兼容设置时会报错，不会自动关闭其他优化。

新实验使用 `schema_version: 4`。运行仍通过 `clawbox experiment launch`；
`configure` 生成可编辑实验臂，`describe` 展示各臂实际机制，配方名和臂名不决定运行行为。
从 [主机场景 walkthrough](self-service.md) 完成安装、host apply 和最小实验后继续。

高并发短连接的宿主网络配置使用 host YAML 的 `tcp_tw_reuse`（默认 `null`，保持现状；
`0` 禁用、`1` 全局、`2` 仅 loopback）。修改后 host apply，重启后重新 apply；无需镜像重建或重训。
它作用于宿主网络命名空间，所有对比臂必须保持一致，并使用新 run ID。
仅在确认 TIME_WAIT/端口耗尽瓶颈后选择相应值；操作与恢复步骤见 walkthrough。

`experiment launch SPEC --apply-host HOST.yaml` 可在同一入口应用已审阅的主机配置并运行；
默认不 apply。主机 profile 保存到 HOST 同目录同名 `.json`，要求主机无运行 VM。
同一 run ID 已运行/已成功时不会重复 apply；容量、NUMA 或网络设置改变后使用新 run ID。
无需因此重建镜像或重训。重启/中断恢复顺序见 walkthrough。

## 配置结构

| 区域 | 内容 |
|---|---|
| `environment` | `runtime`、`sandbox` 模板与规格，`resources` 中的 CPU/NUMA、LOCAL/WARM 容量、借用上限、存储路径、紧急内存底线 |
| `workload` | 任务、独立工作区、每 case 的模板与复用概率、会话分配、重复次数 |
| `inference` | API/replay、模型与 `time_scale`；真实 LLM 等待不作为调度预测 |
| `evidence` | Tool 资源预测文件、回收/借用/冷热恢复成本及来源 |
| `measurement` | 采样间隔、稳定等待、只观测预测开关、最终任务验证与输出 |
| `mechanisms` | 六类机制，以及单独的 deployment 部署方式和 coordination 跨机制决策 |
| `conditions` | 所有实验臂共享的预测来源；不能通过臂内 override 改变 |
| `comparison` | 配方标签与显式 `arms`；每臂用 `override` 修改机制字段 |
| `execution` | 并发、到达方式、随机种子、顺序与超时 |

所有字段的类型、默认值和取值约束可直接查询：

```bash
bash scripts/clawbox experiment recipes
bash scripts/clawbox experiment schema > "$HOME/clawbox-specs/schema.json"
```

示例入口为 [six-dimension.yaml](../examples/experiments/six-dimension.yaml)。示例模板是占位符，
`configure --profile host.json` 从已应用的主机配置填入真实身份。不会从物理容量推断可用容量，
也不会自动安装 guest 依赖。

## 六个维度

以下路径均相对于 `mechanisms`。MiB=1024² 字节，时间为秒；容量相关物理字段仍在 environment。

| 维度 | 主选项与默认值 | 实际作用与依赖 |
|---|---|---|
| `admission` | `tool: off`，可选 fixed/predicted | 控制工具开始；账本只补计尚未体现的增长。预测不是硬上限 |
| `runtime` | `memory_protection: false`、`cpu_release_boost: false`、`proactive_memory_reclaim: false` | 独立 VM 控制；CPU 提升依赖实际争用与释放历史。主动内存回收使用 memory.reclaim，不保证只回收缓存 |
| `placement` | `policy: round_robin`，可选 os_default/explicit/load_aware；`template_affinity: false` | 模板亲和要求 load_aware 和 compute_nodes，并自动要求物理共享验证。显式放置填写 session_compute_nodes |
| `idle_reclaim` | `policy: off`，可选 wait；`scope: tool`，可选 agent_and_tool | wait 在真实 LLM 请求未完成、无 Tool 执行且存在内存压力时回收；agent_and_tool 同时回收 Runtime 和 Tool。LLM 响应到达时恢复 Runtime，下一次 Tool 准入时恢复 Tool |
| `shared_memory` | `borrow: off`，可选 hard/pressure | 借用先预留配置容量。pressure 支持常驻 VM，也支持与回收组合。归还 LOCAL 后若仍有共享驻留页，保留容量预留；该 VM 再次借用时复用已有预留 |
| `checkpoint` | `mechanism: incremental-cow`，可选 full-copy；`storage: disk`，可选 warm-only/tiered；`residency: lru`，可选 value | value 要求 tiered、实测冷热恢复成本和每 case 复用概率。页面加载由后端机制决定，没有独立 eager/lazy 开关 |
| `deployment` | `mode: managed`，可选 os-default | 部署基线。managed 按配置容量预留创建/恢复需求；os-default 不做这种预留，且要求 OS 放置、常驻、关闭工具准入、运行时控制、借用及模板亲和 |
| `coordination` | `action_selection: reclaim_first`，可选 measured_cost | 跨借用与回收的动作选择；measured_cost 要求 pressure 借用、空闲回收和实测动作成本 |

未启用 idle_reclaim 时，checkpoint 配置不产生检查点。启用借用不自动启用回收；
CPU/内存控制不自动启用借用、回收或模板亲和。运行内存借用和 WARM 快照仍共用一个原子容量账本。

`conditions.prediction_source` 默认 `kb`，正式预测实验使用冻结 KB 查询，评估期间不训练。
`command-table` 只用于精确命令查表诊断；`describe` 会提示此条件，所有臂使用同一来源。
使用 `--set conditions.prediction_source=command-table` 创建单独的诊断配置，不将其混入 KB 准入消融。
`idle_reclaim.restore`、`placement.template_base_sharing`、`checkpoint.lazy_restore` 不再是公开选项。
模板亲和会自动要求共享验证；未开启亲和不代表禁止了后端物理共享。
内部保留 WARM 文件及容量记账，不把文件释放假设包装成 eager/lazy 恢复开关。

重要参数：

准入对可运行请求保持先来先服务，并优先已有会话的进展。同一 VM 仍有执行中的操作时，后续请求等待该操作结束，不阻塞其他 VM；准入等待时间也包含这种同 VM 串行等待，不能全部解释为内存不足。
负载感知放置的内存排序观测以 0.2 秒为周期共享，避免等待会话重复读取全部 VM；每次实际预留仍重新检查当前内存预算。放置事件记录观测年龄。
创建队列先取得创建槽，再做放置和内存预留，避免尚不能创建的 VM 占用准入预算。VM 对就绪后立即释放创建槽，槽数不限制已运行会话数。
`CLAWBOX_SANDBOX_CREATE_CONCURRENCY` 设置同时创建的 VM 对数，默认 8，必须为正整数，并受本次会话数限制；修改后使用新 run ID，无需 host apply、重建镜像或训练。

| 路径 | 默认值 | 单位/用途 |
|---|---:|---|
| admission.static_tool_memory_mib | 未设置 | 固定准入额度 MiB；configure 的 `--static-tool-memory-mib auto` 从独立训练产物取实测 P90 |
| admission.prediction_reservation_scale | 1 | host 校准后预测额度的倍数 |
| admission.non_command_tool_memory_mib | 未设置 | MiB；文件接口和 SSH 后端维护操作的公共固定预算，未设置时用 static_tool_memory_mib；fixed/predicted 均不对它应用预测缩放 |
| admission.admission_margin_mib / startup_headroom_mib | 256 / 256 | 全局准入余量/创建余量 MiB |
| idle_reclaim.checkpoint_restore_headroom_mib | 1024 | LOCAL 内的生命周期操作余量 MiB |
| runtime.runtime_poll_interval_seconds | 0.5 | 控制采样周期；也用于新控制器 |
| runtime.pressure_control | 见下文 | 压力控制与释放判定参数 |
| placement.placement_cpu_overcommit | 8 | offered vCPU/允许 CPU 数倍率，不是测得利用率 |
| placement.template_balance_slack | 0.10 | 模板亲和相对于最佳压力分数的允许偏差 |
| placement.tool_call_cpu_policy | c0 | `c0` 严格保留 8c555ae 的默认 CPU 行为：VM 使用已选 NUMA 的完整 CPU 集，由 Linux 调度且不增加拓扑采样。`c2` 仅在 Tool 调用期间，把 Tool VM 的 vCPU 线程限制到一个完整 `cluster_cpus_list` 物理核组；例如 `0-7` 去除 SMT 重复后为 `0,2,4,6`。Tool vCPU 数只规定候选 cluster 必须具有的最少独立物理核数，不切分 cluster。要求 Linux 可读 cache/cluster/SMT topology，且选定 NUMA 至少有两个完整候选 cluster，否则非零失败。调用结束、失败或响应丢失时恢复完整 NUMA mask。需要新 run ID；不需要 host apply、镜像重建或重训 |
| placement.tool_call_cpu_pressure_ratio / tool_call_cpu_pressure_samples / tool_call_cpu_cooldown_seconds | 0.10 / 2 / 5 | C2 的 VM CPU PSI stall 比例、连续样本数和冷却秒数；按 `measurement.memory_sample_interval_seconds` 周期采样。活动调用持续超阈值时单向放宽到完整 NUMA。缺少 PSI 时也放宽并记录原因。C0 不读取这些观测 |
| shared_memory.borrow_cooldown_seconds / return_cooldown_seconds | 5 / 5 | 借用/归还冷却时间 |
| checkpoint.pool_transfer_concurrency / pool_transfer_timeout_seconds | 1 / 120 | 全池生命周期操作并发上限/等待期限；不是带宽 |
| measurement.memory_sample_interval_seconds / stabilization_seconds | 0.2 / 1 | 内存采样/稳定等待 |
| measurement.observe_predictions | false | 准入关闭时仍观测预测；需要匹配的预测文件 |

`runtime.pressure_control` 的默认值为：psi_ratio=0.10，exit_hysteresis_seconds=1，
cooldown_seconds=10，action_window_seconds=2，protection_budget_mib=256，max_boosted_vms=1，
boost_cpu_weight=200，min_release_mib=64，amortization_factor=2，release_history_samples=3，
release_forecast_ttl_factor=3（大于 1 的有限倍数）。CPU 释放预测从执行开始起最多有效
“历史最长耗时 × 该倍数”，预计完成时间另用历史耗时中位数；超过预计耗时不立即失去加速资格。
每轮仍检查 CPU 争用、准入阻塞、执行身份和实际内存增长。修改该参数需要新 run ID，
不需要 host apply、镜像重建或重新训练。保护窗口结束后进入冷却，冷却后可重新保护；
仍有回收压力不等于保护无效。
保护总量受预算约束，正常 CPU 权重为 100；执行结束、超时或压力消失时复位。
共享内存或跨节点页面不作为释放感知 CPU 提升的证据。
启用内存保护或 CPU 加速时，对应宿主 cgroup PSI 文件缺失会在 VM 预检前报错，
不会静默降级；关闭两项机制的基线不要求 PSI。内核已编译 PSI 但默认关闭时，
需在启动配置中启用 `psi=1` 并重启，再检查接口。CPU 释放历史只记录本次运行的宿主
内存下降；guest 命令结束不代表宿主释放页面。内存保护针对宿主 VM cgroup 回收，
不直接保护 guest 进程工作集。具体适用条件见 [压力控制说明](pressure-control.md)。

关闭回收或借用的单开关消融可保留 `coordination.action_selection: measured_cost`；另一能力仍可用，只有一种可行动作时没有成本排序选择。必须保留独立实测成本，不能因此额外关闭协调或改变物理额度。两种能力都关闭时此设置报错。
关闭操作不移除 `environment.resources` 中的物理共享池容量；它仍约束实际共享 NUMA 驻留，避免跨 arm 的残留缓存被误报为零容量越界。

## 证据输入

- `evidence.prediction_artifact`：独立已验证训练产生的 JSON；runtime 模式还需要匹配的 `.kb.json`。
- 未实现 LLM 时长预测。`release_cost`、预测时长字段及 oracle 策略不受支持；旧配置须删除这些字段，并显式使用 `idle_reclaim.policy: wait`。trace 的真实 LLM 时间仅用于回放等待和事后测量，不参与回收决策。
- `evidence.memory_action_costs`：回收与借用成本比较，字段沿用 [执行成本定义](guide.md#numa-memory-scheduling)。仅 measured_cost 使用，不自动开启借用。
- `evidence.snapshot_residency_costs`：provenance、warm_restore_seconds_per_gib、cold_restore_seconds_per_gib；每个 `workload.cases[]` 另填 `snapshot_reuse_probability`。

WARM 预留与实际快照占用不同：暂停前每台 VM 预留配置 guest 内存加 256 MiB，双 VM 范围先取得两份预留；完成后按快照 manifest 的实际分配量记账。2/4 GiB 的 VM 对因此需要 6.5 GiB 可预留空间。缩小共享池时还要计入已有快照、活跃借用和并发预留；池满不再作为会话失败，容量不足的回收会等待或选择允许的其他动作。

成本单位都是秒/配置 GiB，复用概率范围 [0,1]。冷恢复不慢于热恢复时驻留价值为零，保留该负结果。
模板共享必须通过同文件映射、Shared_Clean/PSS 等实际验证；逻辑 base 依赖不等于物理共享。
PMU 保留现有执行 ID 对齐及四事件质量约束；采集和独立 LLC 放置研究不代表在线 LLC 调度。

progress-enabled Toolbridge 的 `tool_execution_progress` 记录进程执行的 start/end（宿主接收时间）；用 execution ID 与带 `execution_scope` 的 `tool_admitted` 对齐可区分实际在途 Tool 与已准入未启动 Tool。缺失 end 的执行仍须作为未完成报告，不能只计算已结束命令的并发。
`tool_admission_queued` 到同 execution ID 的 admitted/failed 事件表示准入等待区间；它与 VM 创建、放置队列分别统计。

## 配方与受控比较

消融实验建议使用 `configure --fixed-methods --recipe ...`（默认不开启）。它关闭无关策略，
把完整机制写入每个实验臂；保留公共硬件、负载、测量和数值调参。
`--set` 的策略开关也会被固定方法覆盖，以生成后的 `describe` 为准。

| 固定方法族 | 共同背景；比较项 |
|---|---|
| admission | 常驻、无借用；工具准入 off/fixed/predicted |
| runtime | 固定工具准入、常驻、无借用；内存保护 × CPU 加速的四组合 |
| memory-reclaim | 固定工具准入、常驻、无借用；主动内存回收 off/on |
| placement | 无工具准入、常驻、无借用；RR / 负载感知 / 负载感知加模板亲和 |
| idle-reclaim | full-copy/disk；常驻 / Tool 等待回收 / 双 VM 等待回收 / Tool 成本排序 |
| shared-memory | 常驻、无工具准入；借用 off/hard/pressure |
| coordination | pressure 借用、Tool 等待回收、full-copy/tiered/LRU；reclaim_first/measured_cost |
| checkpoint encoding | Tool 等待回收、disk/LRU；full-copy/incremental-cow |
| checkpoint storage | Tool 等待回收、full-copy/LRU；disk/warm-only/tiered |
| checkpoint residency | Tool 等待回收、full-copy/tiered；LRU/value |

除表中指定机制外，统一受管创建、RR、工具准入关闭、三项运行时控制关闭、回收关闭、借用关闭、
reclaim_first，不启用模板亲和或显式节点分配。integrated/custom 不支持固定模式。
runtime/memory-reclaim 使用 `admission.static_tool_memory_mib`（MiB）；未填写时从独立预测训练文件
校准，与 admission 的 fixed 组一致。idle-reclaim 要求独立实测 `evidence.idle_reclaim_costs`。
等待估计、WARM 路径、价值证据、共享基底和 Toolbridge 能力仍需验证；固定模式只接受 KB 预测来源。
生成配置无需 host apply、镜像重建或重训；改变对应的主机设置、镜像能力或训练数据时仍须执行那些步骤。
更换方法配置使用新 run ID。跨方法族背景不同，不应直接视为单因素消融。
关闭内存/CPU 控制时不启动压力轮询，仅成本排序也不启动该轮询。
开启机制仍有必要开销；必须核对实际动作记录，压力不足或缺少重复工具历史时可能不触发。

配方仅生成下表的差异，其余机制来自公共配置。运行前检查 `describe`，确认没有带入不希望研究的优化。

| `--recipe` | 生成的实验臂 | 前提 |
|---|---|---|
| admission | off / fixed / predicted | 预测文件；fixed 未设置时从训练文件读取 host 增长 P90 |
| runtime | memory-off-cpu-off / memory-protection / release-cpu / memory-and-cpu | 四组均明确关闭主动内存回收；其他分组保持公共设置。自适应控制要求 progress-enabled Toolbridge |
| memory-reclaim | memory-reclaim-off / memory-reclaim-on | 只改变主动内存回收；其余开关固定，实际动作还要求准入预算阻塞 |
| placement | round-robin / load-aware / load-aware-template-affinity | compute_nodes；最后一组自动要求共享基底验证 |
| idle-reclaim | resident / wait-tool / wait-pair | 真实未完成模型请求、内存压力和快照容量；不需要 LLM 时长预测 |
| shared-memory | local / hard-borrow / pressure-borrow | 正共享借用额度；三组均固定 reclaim_first |
| coordination | action-reclaim_first / action-measured_cost | 公共 pressure 借用、空闲回收和独立实测动作成本；只改变动作选择 |
| checkpoint | 由 `--checkpoint-axis encoding/storage/residency` 选择 | 公共 idle_reclaim 开启；storage 需要 WARM；residency 需要公共 tiered 和价值证据 |
| integrated | os-default / managed-neutral / configured-design | 先明确填写希望研究的组合；不存在隐式 all-on |
| custom | configured-design | 可编辑 comparison.arms 做单机制或特殊组合 |

`integrated` 的 OS 组继承相同宿主 live cgroup 边界，使用 OS 放置与创建；
managed-neutral 使用受管创建及 round_robin 放置，关闭模板亲和、工具准入、运行时优化、借用和回收。
两者不是只差一个优化的消融。要单独衡量某技术，使用对应配方或只改一列的自定义矩阵。

示例：运行时资源控制比较。

```bash
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/smoke.yaml" \
  "$HOME/clawbox-specs/runtime.yaml" --recipe runtime --concurrency 2 --repetitions 1
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/runtime.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/runtime.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/runtime.yaml" --run-id runtime-01
bash scripts/clawbox --sudo experiment report runtime-01
```

上例是功能检查。性能研究需使用完整真实工作负载、独立训练数据和重复试验，分别建立低压力、
LOCAL 饱和、CPU+内存争用条件。保留失败样本，优先比较完成率、Agents/s 和从到达到完成的延迟。

公共参数可以用 `--set` 编辑，值按 YAML 解析：

```bash
bash scripts/clawbox experiment configure "$HOME/clawbox-specs/runtime.yaml" \
  "$HOME/clawbox-specs/runtime-tuned.yaml" \
  --set runtime.pressure_control.boost_cpu_weight=250
```

每臂覆盖使用同样的点分路径，例如：

```yaml
comparison:
  recipe: custom
  arms:
    - name: fixed
      override: {admission.tool: fixed}
    - name: predicted
      override: {admission.tool: predicted}
```

C0/C2 使用同一份配置和同一次矩阵展开，只覆盖一个策略字段：

```yaml
mechanisms:
  placement:
    tool_call_cpu_policy: c0
comparison:
  recipe: custom
  arms:
    - name: c0
    - name: c2
      override: {placement.tool_call_cpu_policy: c2}
```

不要在 C2 臂覆盖 `sandbox.vcpu`、CPU pool、quota/weight、内存、准入或工作负载。
未显式设置时 Tool 沙箱仍使用 `sandbox.vcpu: 2`；C2 不修改 vCPU 限额，
只要求完整候选 cluster 至少包含相同数量的独立物理核。

修改公共值不会覆盖某臂已有的显式 override；重新选择 `--recipe` 会重新生成 arms。
不支持对 environment 做臂内覆盖，架构变化用不同配置。schema-3 的 suite/baseline 不与新设计混用；
旧配置可作为 `configure --recipe ...` 的工作负载和主机参数输入，但不会继承其隐式策略语义。
旧 `baselines` 命令按 comparison family 标注预设；同一预设可作为多个族的共同对照，
不同 family 不能直接当作单因素比较。`predicted-wait-snapshot` 在真实模型等待且存在内存压力时
回收 Runtime 和 Tool VM；不预测剩余 LLM 等待时间。其 predicted 指 Tool 资源预测准入。

## 生效与验证

单个 Agent 的 OOM、命令超时、验证失败或普通异常只结束该 Agent；其余 Agent 继续运行。
`correctness.session_failures` 保存逐个失败原因，成功率分母仍为全部提交的 Agent。
该臂完成并验证清理后，矩阵会继续下一臂，不会重试直到成功。
共享控制器故障、无法完成资源清理、显式 abort 或 `execution.arm_timeout_seconds`
到期会中止整臂，记录 `correctness.arm_interrupted`。命令超时不等于整臂截止时间。
`execution.arm_finalization_timeout_seconds`（秒，默认 300）只供截止后的 VM 清理和部分结果落盘，
不延长 Agent 工作时间，也不计入工作负载窗口；修改后使用新 run ID，无需 host apply、重建镜像或重训。
升级失败隔离逻辑后使用新 run ID；不需要重新应用主机配置、重建镜像或重训。

机制参数、并发、等待/成本证据改变：新 run ID。物理容量、NUMA、CPU 或挂载改变：host check/apply 后重新 configure。
模板或 guest 依赖改变：重建/注册模板；预测测量环境改变：重新训练。控制器拆分本身不要求重建镜像；
首次使用进度控制需要包含对应 Toolbridge 的镜像。

`launch` 对每种实际快照/借用配置做资格验证，相同探测配置复用结果；检查点编码比较不会只验证第一组。
冻存配置保留完整六维参数，resume 使用同一矩阵。输出已存在需要新路径或明确 `--force`；配置改变使用新 run ID。
没有快照、借用等依赖时不会自动启用它们。未知字段和不支持的组合以非零状态退出。

本地 `python -m pytest` 验证逻辑；`validate --inputs` 检查输入；`qualify` 和实际 smoke 验证 VM 链路。
这些与干净主机安装验证是不同证据。自助安装见 installation.md，验证记录放在用户文档之外。
