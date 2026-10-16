# 六维实验配置与配方

六维表示优化方向，不代表任意组合都成立。主机容量、NUMA 汇总与借用额度先统一校验，再生成实验组；所有组共享同一物理 live 上限。
`runtime.cpu_release_boost` 要求固定 LOCAL NUMA 放置，不能与 `placement.policy=os_default` 组合。
`idle_reclaim.policy=off` 时，快照编码、lazy restore、存储与驻留不生效，`describe` 会提示；resident 对照组可保留这些公共参数。
只借用、不回收时不要求快照路径。选择 checkpoint 配方必须先显式开启空闲回收，避免生成没有快照操作的比较。

两个参数组跨维度共享：`runtime.pressure_control` 也提供 release-cost 排序阈值；`checkpoint.pool_transfer_*` 也限制共享内存传输。
比较相关机制时应保持这些参数不变。配方只修改列出的字段；继承了不兼容设置时会报错，不会自动关闭其他优化。

新实验使用 `schema_version: 4`。运行仍通过 `clawbox experiment launch`；
`configure` 生成可编辑实验臂，`describe` 展示各臂实际机制，配方名和臂名不决定运行行为。
从 [主机场景 walkthrough](self-service.md) 完成安装、host apply 和最小实验后继续。

## 配置结构

| 区域 | 内容 |
|---|---|
| `environment` | `runtime`、`sandbox` 模板与规格，`resources` 中的 CPU/NUMA、LOCAL/WARM 容量、借用上限、存储路径、紧急内存底线 |
| `workload` | 任务、独立工作区、每 case 的模板与复用概率、会话分配、重复次数 |
| `inference` | API/replay、模型与 `time_scale`；等待预测放在 evidence |
| `evidence` | 预测文件、模型等待估计及来源、回收/借用/冷热恢复成本 |
| `measurement` | 采样间隔、稳定等待、只观测预测开关、最终任务验证与输出 |
| `mechanisms` | 六个维度的公共机制参数 |
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
| `admission` | `tool: off`，可选 fixed/predicted；`lifecycle_admission: configured-capacity`，可选 os-managed | 控制工具开始、VM 创建与恢复；账本只补计尚未体现的增长。预测不是硬上限 |
| `runtime` | `backend: neutral`，可选 agentcgroup；`memory_protection: false`、`cpu_release_boost: false`、`proactive_cache_reclaim: false` | 独立开关。AgentCgroup 不与 flat per-VM 内存/CPU 控制混用。CPU 提升依赖实际 CPU 争用与释放历史 |
| `placement` | `policy: round_robin`，可选 os_default/explicit/load_aware；`template_affinity: false`；`template_base_sharing: disabled` | 模板亲和要求 load_aware、required 共享验证及多个计算节点配置。显式放置填写 session_compute_nodes |
| `idle_reclaim` | `policy: off`，可选 wait/release_cost；`scope: tool`，可选 agent_and_tool；`restore: reactive` | wait 支持两种范围；release_cost 只支持 Tool。不能中断活跃命令；需要独立模型等待估计 |
| `shared_memory` | `borrow: off`，可选 hard/pressure；`action_selection: reclaim_first`，可选 measured_cost | 借用先预留配置容量。pressure 支持常驻 VM，也支持与回收组合；measured_cost 要求回收、pressure 和实测成本 |
| `checkpoint` | `mechanism: incremental-cow`，可选 full-copy；`lazy_restore: true`；`storage: disk`，可选 warm-only/tiered；`residency: lru`，可选 value | 增量 CoW 必须按需恢复。value 要求 tiered、实测冷热恢复成本和每 case 复用概率 |

未启用 idle_reclaim 时，checkpoint 配置不产生检查点。启用借用不自动启用回收；
CPU/内存控制不自动启用借用、回收或模板亲和。运行内存借用和 WARM 快照仍共用一个原子容量账本。

重要参数：

| 路径 | 默认值 | 单位/用途 |
|---|---:|---|
| admission.prediction_mode | runtime | 查询冻结 KB；frozen 为精确命令查表，评估均不训练 |
| admission.static_tool_memory_mib | 未设置 | 固定准入额度 MiB；configure 的 `--static-tool-memory-mib auto` 从独立训练产物取实测 P90 |
| admission.prediction_reservation_scale | 1 | host 校准后预测额度的倍数 |
| admission.admission_margin_mib / startup_headroom_mib | 256 / 256 | 全局准入余量/创建余量 MiB |
| idle_reclaim.checkpoint_restore_headroom_mib | 1024 | LOCAL 内的生命周期操作余量 MiB |
| runtime.agentcgroup_poll_interval_seconds | 0.5 | 控制采样周期；也用于新控制器 |
| runtime.agentcgroup_event_threshold / pressure_ratio / protection_window_seconds | 1 / 0.85 / 1 | 完整字段名保留 `agentcgroup_` 前缀；只影响旧 AgentCgroup |
| runtime.pressure_control | 见下文 | 压力控制与释放判定参数 |
| placement.placement_cpu_overcommit | 8 | offered vCPU/允许 CPU 数倍率，不是测得利用率 |
| placement.template_balance_slack | 0.10 | 模板亲和相对于最佳压力分数的允许偏差 |
| shared_memory.borrow_cooldown_seconds / return_cooldown_seconds | 5 / 5 | 借用/归还冷却时间 |
| checkpoint.pool_transfer_concurrency / pool_transfer_timeout_seconds | 1 / 120 | 全池生命周期操作并发上限/等待期限；不是带宽 |
| measurement.memory_sample_interval_seconds / stabilization_seconds | 0.2 / 1 | 内存采样/稳定等待 |
| measurement.observe_predictions | false | 准入关闭时仍观测预测；需要匹配的预测文件 |

`runtime.pressure_control` 的默认值为：psi_ratio=0.10，exit_hysteresis_seconds=1，
cooldown_seconds=10，action_window_seconds=2，protection_budget_mib=256，max_boosted_vms=1，
boost_cpu_weight=200，min_release_mib=64，amortization_factor=2，release_history_samples=3。
保护总量受预算约束，正常 CPU 权重为 100；执行结束、超时或压力消失时复位。
共享内存或跨节点页面不作为释放感知 CPU 提升的证据。

## 证据输入

- `evidence.prediction_artifact`：独立已验证训练产生的 JSON；runtime 模式还需要匹配的 `.kb.json`。
- `evidence.model_wait_prediction_seconds` 与 `model_wait_prediction_source`：独立来源的预计模型等待；新入口不接受从评估 trace 自动推导。
- `evidence.idle_reclaim_costs`：provenance、warm_checkpoint_seconds_per_gib、warm_restore_seconds_per_gib，以及使用磁盘时的 cold_checkpoint_seconds_per_gib、cold_restore_seconds_per_gib。缺少预先成本时，release_cost 等待成功的生命周期观测；未触发不能称为优化收益。
- `evidence.memory_action_costs`：回收与借用成本比较，字段沿用 [执行成本定义](guide.md#numa-memory-scheduling)。仅 measured_cost 使用，不自动开启借用。
- `evidence.snapshot_residency_costs`：provenance、warm_restore_seconds_per_gib、cold_restore_seconds_per_gib；每个 `workload.cases[]` 另填 `snapshot_reuse_probability`。

成本单位都是秒/配置 GiB，复用概率范围 [0,1]。冷恢复不慢于热恢复时驻留价值为零，保留该负结果。
模板共享必须通过同文件映射、Shared_Clean/PSS 等实际验证；逻辑 base 依赖不等于物理共享。
PMU 保留现有执行 ID 对齐及四事件质量约束；采集和独立 LLC 放置研究不代表在线 LLC 调度。

## 配方与受控比较

配方仅生成下表的差异，其余机制来自公共配置。运行前检查 `describe`，确认没有带入不希望研究的优化。

| `--recipe` | 生成的实验臂 | 前提 |
|---|---|---|
| admission | off / fixed / predicted | 预测文件；fixed 未设置时从训练文件读取 host 增长 P90 |
| runtime | neutral / AgentCgroup / memory / CPU / memory+CPU | 新控制器需要 progress-enabled Toolbridge；默认不启用回收 |
| placement | round-robin / load-aware / template-aware | compute_nodes；最后一组要求共享基底验证 |
| idle-reclaim | resident / wait-tool / wait-pair / release-cost-tool | 独立等待估计、快照路径；成本排序可提供预先成本 |
| shared-memory | local / hard-borrow / pressure-borrow / cost-selection | 正共享借用额度；最后一组要求公共 idle_reclaim 开启与动作成本 |
| checkpoint | 由 `--checkpoint-axis encoding/storage/residency` 选择 | 公共 idle_reclaim 开启；storage 需要 WARM；residency 需要公共 tiered 和价值证据 |
| integrated | os-default / managed-neutral / configured-design | 先明确填写希望研究的组合；不存在隐式 all-on |
| custom | configured-design | 可编辑 comparison.arms 做单机制或特殊组合 |

`integrated` 的 OS 组继承相同宿主 live cgroup 边界，使用 OS 放置与创建；
managed-neutral 使用受管创建及公共放置策略，关闭模板亲和、工具准入、运行时优化、借用和回收。
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

修改公共值不会覆盖某臂已有的显式 override；重新选择 `--recipe` 会重新生成 arms。
不支持对 environment 做臂内覆盖，架构变化用不同配置。schema-3 的 suite/baseline 不与新设计混用；
旧配置可作为 `configure --recipe ...` 的工作负载和主机参数输入，但不会继承其隐式策略语义。

## 生效与验证

机制参数、并发、等待/成本证据改变：新 run ID。物理容量、NUMA、CPU 或挂载改变：host check/apply 后重新 configure。
模板或 guest 依赖改变：重建/注册模板；预测测量环境改变：重新训练。控制器拆分本身不要求重建镜像；
首次使用进度控制需要包含对应 Toolbridge 的镜像。

`launch` 对每种实际快照/借用配置做资格验证，相同探测配置复用结果；检查点编码比较不会只验证第一组。
冻存配置保留完整六维参数，resume 使用同一矩阵。输出已存在需要新路径或明确 `--force`；配置改变使用新 run ID。
没有快照、借用等依赖时不会自动启用它们。未知字段和不支持的组合以非零状态退出。

本地 `python -m pytest` 验证逻辑；`validate --inputs` 检查输入；`qualify` 和实际 smoke 验证 VM 链路。
这些与干净主机安装验证是不同证据。自助安装见 installation.md，验证记录放在用户文档之外。
