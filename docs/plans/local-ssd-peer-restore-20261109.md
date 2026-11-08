# 两节点本地 SSD、共享 DRAM 与统一恢复调度：独立实施交接

## 用户补充的正式实验约束（2026-11-09）

以下约束优先于本文后续关于缩小正式实验规模的建议。正式实验固定为 **1280 个 session：从 503 个 base 中固定选取 80 个，每个 base 启动 16 个独立 rollout**；暂以同一原始 trace 的 16 次重复表示这 16 个 rollout，不要求全部 503 个 base 出现。按正态分布生成 80 个 base 组的到达时刻并依次投放，同组 16 个 session 同批到达。不设置额外的活跃 session 或 base 组数量上限，保留原有资源 admission。

每个 session 保持 Runtime＋Tool 结构；同组只共享不可变 base，运行内存、rootfs 私有修改和 checkpoint 状态各自独立。保留真实工具行为和原始 LLM 时间。所有对照使用相同的 80 个 base、到达序列、随机种子、资源预算和初始副本分布。报告实际 base 复用、跨节点传输字节、运行并发、吞吐、完成任务数和创建/checkpoint/restore 时延。小规模测试仅用于能力验证，不能代替正式性能实验；统计分析以 80 个 base 组为相关性单位，不能把重复的 1280 条 trace 当作 1280 个独立任务样本。

## 1. 接手任务与完成定义

本文件是完整实施说明，接手 agent 无需阅读此前聊天。仓库：`C:\Users\29068\Desktop\ClawBox`；远端：`ssh kunpeng`。先阅读仓库 `AGENTS.md`，只扩展受支持的 `clawbox experiment`、CubeSandbox 和 ClawTune 集成，不扩展已废弃控制平面。

用户已确定的设计：

- NUMA 0、1 模拟两个计算节点，各有独立的本地 SSD 和文件系统；本场景不使用共享 SSD/NFS。
- **保留 NUMA 2 的共享 DRAM**，用于 base 热缓存、WARM 快照和运行内存借用。
- 跨节点缺失对象经真实网络传递，模拟总带宽竞争；目标恢复必须实际使用传到目标的对象。
- 恢复可以重新选择节点。现有 NUMA、模板感知、快照、内存回收等策略合并完善，不做互相排斥的替代实现。
- 保持真实工作负载、LLM replay 时间和到达节奏；优先简单有效的机制与可核验实验，不建设工业级分布式平台。

原请求时间窗：北京时间 **2026-11-09 02:18—08:18，最多六小时**。交接时间计入窗口，接手后使用剩余时间，不重新计算六小时。优先实际数据路径、主工作流和有效对照，避免反复重跑已通过的小测试。

**交接状态：仅完成设计及代码/主机只读审查；本文件的新接口尚未实现，未安装新组件或启动本轮实验。** 标为“拟新增”的接口/字段不是现有功能。接手者应实施并验证，不能以计划或编译成功代替完成。

完成意味着：可重建代码/补丁、公开配置和操作命令、真实 VM 双向恢复证据、普通真实 trace 工作流中的调度对照、可回滚安装与可核验结果。负收益可接受；未完成项必须说明，不能用旧复制代理冒充迁移。

## 2. 必须保留的优化与行为

| 能力 | 本次要求 |
|---|---|
| 快速创建 | 保留官方 145 内核、IRQ route batching、仅激活请求的 legacy IRQFD、cgroup birth、TAP 预创建；本地命中不增加全模板复制/逐 VM 全文件哈希 |
| 模板/rootfs | 节点内继续 reflink/CoW；跨文件系统禁止 reflink。相同模板可在两个 SSD 各有副本，内容身份与路径分离 |
| base WARM | node2 按不可变内容共享一份 RAM base；两个 SSD 的 inode 可登记为同一 base 的本地别名，WARM 只计费一次 |
| 快照 | 保留 incremental-cow、有效 RAM lineage、zstd-page、lazy restore；不能默默改 full-copy |
| 内存策略 | 保留 admission、压力回收、借用/归还、运行时控制、WARM 保留和淘汰；新增目的节点成本作为输入 |
| CPU 策略 | 保留 NUMA/cpuset、LLC、idle/core；迁移后按目标重新验证 CPU/cgroup |
| 测量 | 配置、预留、预测和实际消耗分开；不改 replay 时间或到达来改善结果 |
| 数据 | 保留旧模板、源快照、冻结实验、ANN 数据和旧源码；新目录、新 run ID，不覆盖历史结果 |

历史创建 p50/p90 **0.504/1.128 s** 是 2131 个匹配 VM 的结果；全部 2560 个初始 VM 为 **0.610/1.411 s**。这是快路径参考，不是每个 VM 必须低于一秒的保证；小规模新实验不能直接宣称与其等价。

## 3. 已核实的代码、输入与主机状态

### 3.1 起点和位置

| 内容 | 位置/身份 |
|---|---|
| ClawBox 审查起点 | HEAD `b7cd165`，base template in WARM；前创建基线 `366a59a`。先核对 status，保留其他修改 |
| 标准基线 | `.artifacts/experiment-standards/kunpeng-creation-20261108/manifest.json` |
| 远端标准配置 | `/home/weitianc/.config/clawbox/standards/creation-145-20261108/{host.yaml,host.profile.json,experiment.reference.yaml}` |
| 真实 workload | `/data/clawbox-full1280-create-diagnostic-20261108/kernel145-nodisplay-recovery-20261108t0943utc/prepared/workload.json` |
| 正式 spec/共享证据 | 上述远端 run 根下 `sharing-proof/experiment-proof-bound.yaml`、`sharing-proof/template-sharing.json` |
| Python | `/home/weitianc/clawbox-hostkb-20261101/venv/bin/python` |
| 保存的机器设置 | `/home/weitianc/clawbox-hostkb-20261101/machine.env`；通过 wrapper 加载，不打印内容/凭据 |
| 当前 SDK import | `/home/weitianc/CubeSandbox/sdk/python/cubesandbox/__init__.py`，与 native 构建目录不同 |
| 最新完整 native 源码 | `/home/weitianc/clawbox-base-cache-20261108/CubeSandbox`，upstream HEAD `d0081641c59822e4e5653b7462e914410b81910a`，另有重要未提交补丁 |
| 实际安装 Shim | `/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs` |
| 当前 Shim SHA256 | `4e43d2ba0322fd5782f0eb6827570f85c25a4a15256d9d20f38e07951193bd43`，包含最新 base cache 与此前优化 |
| 旧标准 Shim SHA256 | `24764af7e14e93acdf5400b6947c6add3ca371fa7b0c9984df41b623d95eabe3`，早于 base cache；不能因旧 manifest 自动退回此版本 |
| 可复现 native 补丁入口 | `deploy/cubesandbox/prepare-semantic-source.sh`，保留完整有序 patch 链 |
| 最近 base-cache 验证 | `.artifacts/base-template-warm-cache-20261108/REPORT.md`：正确性已验证，没有稳定生命周期加速结论 |

真实 workload 的 case-specific Tool 模板映射必须保留，不能替换成默认 generic Tool 模板。历史有 503 个 case 模板；本轮数量/身份以选中的 workload/catalog 为准。预热应覆盖实际使用的模板，不能只缓存不会使用的 generic Tool base。

本地 `.tmp-review-cube-source`、`.tmp-agent-rl-cube-check*` 仅是部分审查副本，不是完整构建输入。源 HEAD 相同不证明二进制相同；记录源码 diff、patch 清单、SDK import 和各组件 SHA256。

### 3.2 必须重新检查的状态

以下是 **2026-11-09 02:18—02:24 北京时间**的只读观察，不是接手时保证：

- 主机约 02:17:40 重启；boot ID `bc61ed0f-b007-45c3-b73a-393784ae31c6`。本次 agent 没有发起重启，未判断原因。
- 内核 `6.6.0-145.1.28.165.oe2403sp1.aarch64`；Cubelet inactive，CubeMaster/CubeAPI/cube-egress active。
- `/sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events` 缺失；缺失不能当作 VM 池空闲。
- `/mnt/clawbox-pool` 当时未挂载预期 tmpfs，路径落在根文件系统。**验证 mount/NUMA 前禁止写 WARM/base 数据。**
- CubeAPI `LimitNOFILE=524288`。

接手记录 `date -u`、`uptime -s`、`uname -r`、`/proc/sys/kernel/random/boot_id`，核对服务、VM/进程、挂载/UUID和容量。用现有恢复入口恢复标准环境；不额外重启机器，不改 PCIe、BMC、默认内核或 display-off。若再次断联，保留证据，中断 arm 不作为完整性能样本。

### 3.3 SSD 空间是实际约束

| 位置 | 当时底层 | 当时可用 | 约束 |
|---|---|---:|---|
| `/mnt/clawbox-data` | `/dev/nvme5n1p1`，XFS，NVMe5 | 约 42 GiB，99% 已用 | 可放受控的小型新镜像，须先量依赖与留白 |
| `/mnt/clawbox-spill` | `/dev/nvme10n1p1`，XFS，NVMe10 | 约 52 GiB，99% 已用 | 与上一行物理设备独立，同样先预算 |
| `/`、`/data` | LVM/XFS，跨多个设备 | 约 6 TiB | 可放源码/结果/控制状态；在此建两个 loop 不能证明独立 SSD 性能 |
| `/mnt/clawbox-cube` | `/dev/nvme2n1p1` | 未作为可用预算确认 | 旧模板存储，保留 |
| ANN 两个挂载 | `/mnt/ann-recovered-volume`、`/mnt/ann-diskann`，NVMe3/7 | 无关 | 不使用、不删除 |

未挂载设备也可能已有数据，不能当空盘。receipt 保存 UUID、mount、`findmnt`、`lsblk` 物理拓扑和时间，不能只靠设备名。

两个 99% 已用文件系统不能直接当健康新 Cube 池。`clawbox/experiments/cube_capacity.py` 的 `disk_usage` 检查在预测使用达到 85% 时拒绝；目录配额并不自动解决文件系统级阈值。

**优先方案：分别在这两块已确认 SSD 上建立容量受控、实验专属的 loop-XFS 池。** 只格式化本次新建且有 ownership receipt 的镜像，不格式化已有块设备或已有镜像。按模板依赖、快照、实际 overlay、传输 staging 峰值确定容量，预计使用须低于池的 85%，底层另外保留安全余量。预分配 backing blocks，格式化后核验预留仍在；禁止 sparse 超售、禁止调高拒绝阈值掩盖空间不足。

复用 `snapshot_host.py::_ensure_loop()` 的新建/复用检查；改 `_image_paths()` 为每节点独立 image_path，`check()` 按两个 backing 分别查空间，`apply()` 只创建两个本地池，不创建第三个共享盘。**native 实际 storage root 与容量检查必须指向该域池**，只把代理复制目录放进 loop 没有意义。

空间不足时减真实 case/并发，或明确报告容量阻塞；不删除旧模板/旧实验/ANN。报告说明 loop 层、底层独立设备和其他 I/O，不称为两台真实物理主机。

## 4. 当前实现的关键缺口

| 现状 | 必须补齐 |
|---|---|
| `snapshot_scheduling.py` 的 peer-direct 是 os.read/os.write，本地跨文件系统复制 | 真正经节点端点/socket/聚合限速传缺失对象 |
| `rootfs-payload.txt` 只是 marker，另有模拟 `.cow` 预留 | 传真实冻结 rootfs；容量来自实际模板、overlay、快照、暂存 |
| `_create_vm(package.snapshot_id, target)` 仍用原 Cube catalog | 导入目标 domain；native restore 消费目标对象，禁止原全局 ID fallback |
| Cubelet 按 backend 选单个 XFS manager | storage-domain → engine/pool，贯穿 create/checkpoint/import/restore/delete |
| native XFS import/export 未实现，现有跨节点 import 为 S3 | 补完整依赖导出/导入，不假定 SDK 可从任意复制路径恢复 |
| RestoreConfig 有独立 RAM 和设备 overrides，但实际 config/state loader 接受 file:// | 先传输/登记本地包，再走 native restore；不能把 TCP 帮助文字当实现 |
| 普通 worker 闭包持有初始 domain | 可更新 SessionBinding，迁移 CPU/内存/路由/清理归属 |
| storage 强耦合 branching，后者 resident/admission off | 解耦存储与 workload，让普通真实 trace worker 正常使用压力 checkpoint/admission |

旧 `docs/guide.md` 已将旧路径标为 **Simulated Cross-Node Restore**。旧结果不能改名成新实现证据。首要验收：目标 VMM 实际打开目标 SSD 或明确允许的共享 DRAM backing。

## 5. 数据、对象与 native 接口契约

### 5.1 三层定义

- **LOCAL DRAM**：node0/1 运行内存、恢复后的私有页及实际驻留。lazy restore 仍可依赖 backing files，restore 返回不是删除 backing 的条件。
- **WARM shared DRAM**：node2 容量受控、NUMA-bound tmpfs。两个计算节点可以直接使用同一 RAM base/增量；真实 WARM hit 不必重复经 SSD 传输，但要记实际 backing、容量和 NUMA 来源。
- **COLD local SSD**：各节点自己的模板/rootfs、冷 RAM、元数据与副本。无共享 SSD/NFS fallback。共享 DRAM 不充当隐形共享 rootfs 磁盘。

同主机路径可见不等于该节点拥有对象。路径由 domain allowlist 解析；验收再隔离目标进程 mount/storage view。控制平面可存目录/状态，不能在 `/data` 偷放双方都读的 payload。

### 5.2 完整依赖与身份

拟扩展 manifest/新增 `SnapshotReplica`，至少包含：

```text
snapshot_key = (session_id, role, generation)
object_id = immutable content/version identity, independent of path
storage_domain = node0 | node1 | shared_dram
relative_paths, native_catalog_handle, mechanism, component_identity
dependencies[] = object_id, logical_length, sparse ranges, checksum, alias role
logical_bytes, allocated_bytes, transfer_payload_bytes
state = staging | ready; pins/references; transaction_id
```

包包括真实冻结 rootfs、VM/device/config 状态、`sandbox_spec.json`、`metadata.json`、RAM 文件和 `.lineage.json`、必要祖先/base/有效区间、压缩容器与 manifest。第一版 rootfs 传**完整冻结稀疏对象**；reflink 不等于已有可传递 dirty-block delta。RAM 保留原增量层，允许传所需文件的完整有效稀疏范围，无须再开发新差分算法。

必须处理：

1. reclaim 后各层有效区间共同组成状态，首层未必完整；只传最新 `.mem` 或假定首层完整均错误。
2. lineage external_path、压缩 `groups[].paths`/container 包含绝对路径，导入先全部重定位，再解压；不能写回源路径。
3. 保留逻辑长度、洞、权限及目标内 hardlink aliases；跨文件系统 hardlink/reflink 禁止。不能遗漏洞语义或悄悄全填零。
4. rootfs 通过目标 cubecow engine 的受维护分配/登记发布。native 从 `tpl-<templateID>-rootfs` 查找，任意复制文件名不会自动被消费。
5. 内容身份与 SSD `(dev,ino)` 本地别名分开。WARM 仍验证打开的 inode，但相同内容只缓存/计费一次，别名失效要被发现。
6. 不可变对象在导出/导入/预热时校验一次；本地命中使用受信 receipt/文件身份，不能每次创建全文件哈希。
7. 传输和 lazy 使用期间 pin 源 generation/依赖；只有活动 VM、传输、WARM/后继层引用全部解除后才 GC，不按代号简单删除。
8. 清点 RestoreConfig 引用的其他 host payload：内置 os-image/agent PMEM、kernel，以及附加 disk/pmem/virtiofs/bind mount。component_identity 只能证明兼容，不能提供字节。各域预置并验证所需不可变运行资产，或纳入传输；无法正确重绑定的外部挂载明确拒绝。不能迁移 rootfs/RAM 后仍从原共享 SSD 读取这些遗漏依赖。预置的时间、容量与身份也进入准备清单。

apparent、allocated、预留和 wire bytes 分开；reflink 的 st_blocks 累加不等于唯一物理 extents。使用受控池空间差值/配额或保守预留，声明口径；传输字节不是物理 SSD I/O。

### 5.3 拟实现接口

名称可按原生约定调整，但行为必须相同：

```text
export_paused_generation(session, role, generation) -> immutable_manifest
stage_replica(manifest, target_domain, missing_objects) -> ReadyReplica
restore_from_replica(session, role, ReadyReplica, target_placement) -> native_result
```

ReadyReplica 绑定目标 engine/catalog、完整依赖和校验。先 staging，验证后原子 publish；失败只清理本事务暂存。重复导入复用 ready 对象；并发相同对象去重，独立对象可并行。不能用全节点长锁覆盖传输。

domain 贯穿 SDK/CubeAPI、CubeMaster 路由/持久化、Cubelet/XFS engine、Shim、后续 checkpoint/delete。保留 guest 进程/容器身份，host TAP/vsock/cgroup 由目标 native restore 重建/override。若需要新 sandbox ID，以稳定 session ID 更新全部角色/路由映射；不能假装原 ID 自动迁移。

catalog 的查找与缓存键至少包含 `(backend, storage_domain, snapshot_id)`，磁盘扫描/清理也按域隔离。当前 `snapshot_catalog.go` 缓存主要按 backend 和 snapshot ID；只增加两个 engine 而不改 catalog，仍可能错误命中源对象。

## 6. 真实网络与带宽模拟

两个受控 network namespace 的端点经专属 veth/路由传 TCP；发送端只读自己的对象，接收端只写自己的 staging。共享 WARM 命中按共享内存路径计量，不制造一份无用代理传输。

- 每方向所有连接共享一个总带宽；全双工两方向分别限速，不能每连接独享一份。
- 在传输必经接口用 tc 或等价可核验聚合队列限速/延迟；不对 Tool/VM 人为 sleep。
- 延迟字段为端到端单向附加毫秒，避免两端重复加；记录 MTU、qdisc、offload、真实 RTT/速率。
- 两个大传输重叠的合计速率应受限；统计窗口超过 token-bucket burst。分别保存 payload/协议/interface bytes。
- 配置是上限，实际还受 socket/CPU/SSD 约束，不能把配置 400 Gbit/s 当测得 400 Gbit/s。

聚合限速资格测试先测未限速吞吐，再临时使用低于该吞吐的速率，验证两个重叠大传输的合计速率接近同一个限额、不会翻倍。仅看到实际吞吐低于 400 Gbit/s 不能证明限速生效。通过后恢复并核验冻结的正式速率；临时低速资格数据不计入正式性能对照。

[CloudMatrix384 §3.2—3.3](https://arxiv.org/html/2506.12708v2) 给出 VPC 每节点单向最高 400 Gbit/s、CPU UB 约 160 GB/s。这里模拟 **IP 文件传输平面**，不宣称精确复现 UB；共享 DRAM NUMA 访问另计。参考档 400000 Mbit/s，可补 100000/200000 Mbit/s；若实测低于这些上限，应报告三档可能无区别。旧 1000 Mbit/s、5 ms 不自动沿用。

成本预测至少含链路活动传输/排队、源读/目标写、导入/解压。不要每个候选都假设独占全链路。校准值、配置先验、未知值分开；用简单可解释估计，不新建学习平台。

## 7. 统一调度与状态转移

### 7.1 一个决策器，三项研究消融

创建与恢复共用对象目录、容量检查和候选评分。创建考虑模板，恢复考虑 generation 完整依赖；admission 管运行资格，placement 选节点，压力回收选释放者，retention 选保留副本。

先检查 CPU lease、DRAM/admission、SSD/暂存、native 兼容和依赖可取得性，再估计：

```text
ready_time(node) = max(resource_ready_time, artifact_ready_time)
                   + native_restore_and_reconnect_time
```

仅在资源等待与传输真实并行时用 max；串行则按真实依赖链相加。用事件时间戳防止重复计入重叠阶段。执行位置惩罚要有实际工作负载来源，不从原始 NUMA 纳秒值编造 slowdown。缓存通过 missing objects 减少成本，不重复加缓存奖励；同模板 peer 数只能作为 tie-break，不是实测 hit。

迁移收益超过显式 hysteresis 才行动，初始建议 0.25 s。成本未测时先做校准；保守先验可用于初始选择，但标明来源/未测，不能填成 measured。保留 ClawTune 反馈语义，不读未来 replay LLM 时长。

| 报告名 | 同一调度器中的区别 |
|---|---|
| home | 仅在当前/原节点恢复，其他容量/对象/快照机制一致 |
| load-aware | 可换节点，只按资源就绪排序；仍真实传输/导入且满足全部硬约束 |
| cost-aware | 排序加入缺失依赖、传输竞争、导入/解压/restore，选预计更早可执行的节点 |

三者共用实际恢复路径和相同 base/压缩/cache 机制。cost-aware 合并原模板感知能力；另两者是消融，不是互斥的产品优化实现。

### 7.2 SessionBinding 和迁移边界

拟新增稳定 SessionBinding：session ID/index、epoch、current node/domain、CPU lease、Runtime/Tool lifecycle、transition（stable/staging/restoring/unknown）、pending destination、transaction ID。所有 coordinator/sampler/controller/borrower、priority 校验、回调和最终清理经 binding 查当前归属，不能捕获初始 domain。

第一版只在 **Runtime 和 Tool 都成功 SWAPPED，且无 Tool 执行/增长/待授予 admission claim、无未决生命周期结果**时重选节点。单角色暂停、正在借还、checkpoint 未完均不迁移。不做 live migration，不拆分两个角色。

Runtime 可以先恢复交付 model response，Tool 仍在下次真实请求反应式恢复；两者已经锁定同一目标。保留 wait 状态与模型交付顺序。

### 7.3 事务与资源账本

1. session 锁下确认双 SWAPPED、epoch/generation、无冲突，排名；全局 admission 锁不覆盖 I/O。
2. 原节点获选则按相同 lease 规则走现有快路径。
3. 目标获选：记录 staging，pin 源依赖，短锁预留目标 SSD/staging/传输份额；锁外传输。
4. 校验并 publish ready 包；重查 epoch/generation、目标资源，过时决策不得提交。
5. 经 DemandLedger/目标 coordinator 预留恢复内存，准备 CPU/cgroup，以 pending target 显式恢复。源/目标不能同时执行 workload。
6. 权威成功后原子移交现有 SessionState、scoped claims、lease 和注册，epoch++，更新角色 ID/路由。Runtime relay 就绪再交付响应；Tool 端点按 epoch 重新解析。
7. 释放暂存预留/传输 pin；源副本保留按价值/容量决定，lazy 引用继续保留。
8. native restore 前明确失败可回源，但须记录失败/fallback并释放目标预留；native 结果不明设 unknown，非零失败/受控协调，禁止盲目再启第二份。

CPU lease 独立于 session identity：双角色都 SWAPPED 才释放整对，任一 resident 就保留，首个恢复只重新获取一次整对。所有 baseline 同样执行，避免把容量释放收益错归因节点选择。

**陷阱**：`PolicyCoordinator.unregister()` 会释放 session 的全部共享 ledger claims，迁移不能 unregister/register；原子移交现有 SessionState，只移动相关归属。`PriorityDomain.configure_topology_placement()` 拒绝更换节点，应另加双 SWAPPED 专用 handoff，不能放松运行期约束。

用 run-owned 简单事务记录足够，不建设通用分布式事务系统。权威切换未完成就不能宣称迁移完成。

## 8. 修改地图及公开 schema

路径相对 ClawBox；native 路径相对完整 CubeSandbox。行号仅定位参考。

| 文件/符号 | 工作 |
|---|---|
| `clawbox/experiments/design.py`：Placement、lower_arm/export/recipes/describe | schema 4 新字段 lowering/roundtrip、每 arm 生效，不仅改内部 spec |
| `clawbox/experiments/spec.py`：SnapshotSchedulingStorageSpec/ExecutionSpec/验证 | 普通 worker 可独立配置 storage；新 topology 不要求 NFS/branching，正常 admission/reclaim 校验继续 |
| `clawbox/cli.py`、`snapshot_host.py` | 沿用 snapshot-storage init/check/apply/cleanup，独立镜像/真实网络/共享 DRAM receipt，可重复恢复/preview |
| `snapshot_scheduling.py` | 公共 replica/transport 能力供 worker 共用，去掉原全局 ID 假恢复，缩小长锁 |
| `numa_placement.py`：SessionPlacement | 同一容量/对象候选、CPU lease、迁移评分 |
| `worker.py` 约 2523/2841/3446/3674/5364 | per-domain roots、binding、目标恢复与 Runtime-first，监测/回调/清理同步 |
| `worker.py` 约 729—818、4103—4134 | joint action selector 复用成本；Tool endpoint epoch 重解析 |
| `clawbox/experiments/policy.py` | SessionState/claims 原子 handoff，压力资格/借还竞态 |
| `clawbox/experiments/priority.py` | quiescent handoff、目标 NUMA/CPU/LLC/QoS 校验 |
| `clawbox/cube/{client,lifecycle}.py` | ReadyReplica/destination restore，身份/generation/错误传播 |
| `experiments/{snapshot_pool,base_cache}.py`、`cube/snapshot_compression.py` | replica 位置、pin、内容/别名、WARM去重、路径重定位 |
| `qualification.py`、`supervisor.py`、`reporting.py`、`cube_capacity.py` | target-consumption gate、每域容量、资格身份、owned清理和准确指标 |
| native `Cubelet/storage/{local,snapshot_catalog,cubecow_volume_manager}.go` | domain engine、完整包、catalog/rootfs 发布与解析 |
| native `Cubelet/pkg/cubecow/cubecow.go`、`cubecow/src/engine/mod.rs` | per-engine handle，受维护 XFS 导入/导出能力 |
| native `CubeShim/shim/src/sandbox/sb.rs`、VMM `config.rs/migration.rs` | 复用 native local restore/overrides，不另写 launcher |
| SDK/CubeAPI/CubeMaster、deploy patch recipe | 传递/持久化 domain 与导入身份，组件一致，可重建 |
| `docs/guide.md`、`docs/reference.md`、`examples/` | 操作顺序、默认/单位、安装/恢复/cleanup、何时 apply/rebuild/new ID |

### 8.1 必须处理的 schema/dispatch 细节

- public `mechanisms.placement.resume_policy` 若映射 internal `execution.resume_placement_policy`，须在 Placement 添加 public extra，DesignExecution 排除 internal 字段，lower_arm 显式赋值，PolicySpec.execution_overrides 包含 internal 值，p.mechanisms 保留 public 值供 export。简单加入 PLACEMENT_FIELDS 会暴露错名；遗漏 overrides 会让各 arm 悄悄相同。
- 测 `compile → expand arms → export → reload`，不同 resume_policy 保持不同。describe 展示创建 placement 与恢复 policy。
- `spec.py` 的 branching/storage 等价改成“branching 必须有 storage”；storage 独立校验后，普通流程继续所有 admission/runtime/reclaim 校验。branching 的 early return 只属于真 branching。
- `validate_applied_storage()`、SnapshotStorage 构造和 host apply 当前无条件 NFS，必须按新 topology 修改 receipt/roots；transport 配置放 storage，不能只放 SnapshotBranchingSpec。
- 普通 worker 分流依据 `execution.snapshot_branching`，保持这个条件。不能塞 dummy branching：它会走另一执行路径并绕过普通 OpenClaw gateway/控制设施。
- `affinity/snapshot_aware` 当前是 branching 选择器，普通 worker 只对 load_aware 动态选初始节点。初始 placement 与 resume selector 分开，不冒用旧名称。
- `design.py` 的 tiered/disk 全局 cold_snapshot_root 检查改为接受完整 per-node 映射，并在构造 lifecycle/controller 前使用该域根；不能填一个共享假根过校验。
- 非 home 恢复必须 managed compute nodes、真实 snapshot reclamation、已验证 domain/transport、双角色 scope；拒绝 OS-default、不支持的固定 session slots、Tool-only pause。cost-aware 需成本校准来源。
- storage 存在即触发 qualification/cleanup；别让 branching-only 豁免覆盖普通 worker。普通 smoke 未必发生 pause/move，须单独真迁移 gate。native import 若短暂多出 ID，配额须计入重叠。

### 8.2 拟新增配置契约（尚不能直接运行）

保留 schema 4 实验外层和现有 CLI。可调整最终字段名，但交付时 example/reference/describe 与实现必须一致。

| 字段 | 语义/默认建议 | 变更要求 |
|---|---|---|
| host `schema_version: 2`、`topology: peer-local-ssd` | 严格新 schema，不默默解释旧 NFS 计划 | check/apply、新 receipt |
| host `nodes.<id>.numa_node/cpus` | 与 compute_nodes 对齐、实机核对 | apply/资格 |
| host `nodes.<id>.backing_mount/filesystem_uuid/image_path/local_mount` | 两块独立 SSD、明确自有范围，无猜测默认 | apply，不覆盖已有数据 |
| host `nodes.<id>.capacity_mib` | 必填正数 MiB，先测依赖/预留/85%阈值 | 改容量选择新池 |
| host `nodes.<id>.netns/transfer_address` | 查重的专属 namespace/地址/端口 | apply，不动 SSH 管理网络 |
| host `network.bandwidth_mbit_per_direction` | 必填，十进制 Mbit/s，每方向聚合 | network apply、新 ID |
| host `network.latency_ms_one_way/mtu` | 探索起点 0 ms附加/1500；实际延迟另测 | network apply、新 ID |
| host `shared_dram` | 引用已保存 WARM root、NUMA2、预算/receipt | 验证 tmpfs/noswap/NUMA，不建冲突池 |
| `environment.resources.snapshot_scheduling_storage` | 两域roots/budgets/endpoints、新receipt、shared DRAM；无 branching 强制 | 资格、新 ID |
| storage `transport` | 明确真实 peer-network，区别旧模拟 peer-direct | 资格、新 ID |
| `mechanisms.placement.resume_policy` | home/load-aware/cost-aware，旧普通场景默认 home，新例子显式 cost-aware | lower/overrides、新 ID |
| `mechanisms.placement.migration_threshold_seconds` | 建议0.25s、非负、可配置 | 新 ID |
| transfer并发/超时/成本来源 | 优先复用已有参数，注明每节点/每方向 | 新 ID；限速变更另 apply |

资格 digest/metadata 必须绑定新字段、对象身份、native/SDK。新字段不要求重训 ClawTune；VM 内协议变更才需镜像重建。native 接口需匹配组件重建/资格，不顺便升级整个 Cube 版本。未配置新 topology 的普通实验无需 peer 服务。

## 9. 实施顺序与可用命令

### 9.1 接手先做什么

1. 核对 HEAD/status、AGENTS；独立分支/checkout，不覆盖用户或其他 agent 修改。
2. SSH 只读确认 boot/进程/挂载/空间/身份，把证据放新目录。
3. 恢复现有标准环境，验证无活动 VM 后再安装/host apply/缓存维护。
4. 冻结小真实 case 集合和模板闭包，量空间决定池预算；最先打通 native domain import→target restore。
5. 可并行 native、host/transport、worker/账本三项，先约定接口与文件所有权。

以下 Bash 在远端**本任务 ClawBox checkout 根目录**执行。建议任务路径首次创建前查重，最终写可 source 的 task env（不含凭据）。缺失变量应停止，不猜值。

```bash
PEER_TASK_ROOT=/data/clawbox-peer-local-ssd-20261109
PEER_RESULTS="$PEER_TASK_ROOT/results"
PEER_MACHINE_ENV=/home/weitianc/clawbox-hostkb-20261101/machine.env
PEER_STANDARD=/home/weitianc/.config/clawbox/standards/creation-145-20261108

# 已有恢复命令，不需要重启主机。
sudo bash scripts/recover-cubesandbox-after-reboot.sh 1200

# apply前先核验VM池空闲，profile写本任务新位置。
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment host check "$PEER_STANDARD/host.yaml"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment host apply "$PEER_STANDARD/host.yaml" \
  --profile "$PEER_TASK_ROOT/host.profile.json"
```

wrapper flags `--machine-env/--sudo` 在普通 CLI 参数前。不要打印 machine.env，不复用系统 `$HOME` 等变量存任务路径。脚本会等待 delegated cgroup/存储状态，service active 不是就绪证明。

然后验证 WARM tmpfs/noswap/NUMA2、cgroup populated=0、native/SDK、API NOFILE。保存 `http://127.0.0.1:9998/v1/network/taps` JSON，按实际 schema 统计 Ready且未占用；Full1280 至少2560，其他规模按 Runtime+Tool 数预留，不只检查配置值。

### 9.2 native 构建安装

在新完整源码目录复现完整 patch 链后追加本次 tracked patch。prepare 若拒绝不同 patch 集重叠，用新目录，不强覆已验证树。

准备新树之前，比对当前完整 `/home/weitianc/clawbox-base-cache-20261108/CubeSandbox` 的 diff/未跟踪源码与 recipe 的全部补丁；尚未纳入 recipe 的实际优化须保存为 tracked patch。相同 upstream HEAD 加一次 recipe 调用不能证明等价于已安装的 4e43 版本。做源码清单与针对性语义核对即可，不为此重跑旧性能基线。

```bash
bash scripts/build-cubesandbox-release.sh \
  --assets "${VERIFIED_ASSET_BUNDLE:?set verified asset bundle}" \
  --source "${NEW_CUBE_SOURCE:?set new native source directory}" --jobs 1
```

asset bundle 含已验证 SHA256SUMS、guest images、kernel；本轮未核实具体路径，先从部署记录定位校验，不能编造。必要时用已有 --mirror cn/--direct-network，不更改全机网络。

安装前备份全部实际服务二进制路径/哈希、versioned Shim、配置、SDK 和 unit。`scripts/install-incremental-cubesandbox.sh` full模式现接受 source/project/WARM/COLD，仍有单域假设；**先扩展并审查 domain-aware 安装再调用**。Shim-only 不够。匹配所有受影响 CubeAPI/Master/let/Shim/SDK，保存 installation receipt，核对实际加载版本。

```bash
# SDK更新不代替native安装；明确使用现有Python环境。
/home/weitianc/clawbox-hostkb-20261101/venv/bin/python -m pip install \
  -e "${NEW_CUBE_SOURCE:?}/sdk/python"
/home/weitianc/clawbox-hostkb-20261101/venv/bin/python -c \
  'import cubesandbox; print(cubesandbox.__file__)'
```

只传 tracked source+明确新文件/patch并验SHA，不上传整个历史 artifacts。若SCP/SFTP reset，可参考 `.artifacts/base-template-warm-cache-20261108/upload.py LOCAL REMOTE` 的SSH分块方式，换新名字并核验，不复用失败.part。它只是源码传输辅助，不是实验执行器。

### 9.3 实现后公开工作流

命令形状已有，新schema/语义须先实现。最终生成无占位的 host plan/spec/资格路径/task env/操作说明；不能以这些占位命令作为已执行证明。

```bash
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment snapshot-storage check "${PEER_HOST_PLAN:?}"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment snapshot-storage apply "$PEER_HOST_PLAN"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" \
  --output-root "$PEER_RESULTS" experiment validate "${PEER_SPEC:?}" --inputs
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  --output-root "$PEER_RESULTS" experiment doctor "$PEER_SPEC" --probe-vm
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  --output-root "$PEER_RESULTS" experiment qualify "$PEER_SPEC" \
  --concurrency 2 --receipt "${PEER_QUALIFICATION:?}"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  --output-root "$PEER_RESULTS" experiment launch "$PEER_SPEC" \
  --run-id "${PEER_RUN_ID:?new immutable ID}" \
  --qualification "$PEER_QUALIFICATION" --detach
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" \
  --output-root "$PEER_RESULTS" experiment status "$PEER_RUN_ID"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" \
  --output-root "$PEER_RESULTS" experiment collect "$PEER_RUN_ID"
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  --output-root "$PEER_RESULTS" experiment report "$PEER_RUN_ID"
```

资格并发2仅能力门，不是正式性能验证。receipt须覆盖目标域、当前组件、模板、mechanism、transport和实际消费证据。现有receipt不可无条件复用。

## 10. 必要验收

| 验收 | 最小证据 |
|---|---|
| schema/逻辑 | schema4 roundtrip，普通worker无branching；非法路径/domain/mount/空间拒绝；排序/cache去重正确 |
| 账本/并发 | 双SWAPPED才释放lease，首恢复只获一次；并发不超CPU/DRAM/SSD；handoff不丢wait/claims；旧epoch拒绝 |
| 失败/竞态 | transfer失败源权威，unknown不双启，pin防GC，response/Tool admission竞态使用当前binding |
| 真实网络 | 两个大对象并发合计限速，同对象只传一次；稀疏/校验/不完整包正确 |
| 实际恢复 | A修改RAM/rootfs/进程状态→checkpoint→A到B→native restore核验→再checkpoint→B到A；记录engine/rootfs/RAM mapping |
| 无源捷径 | 源SSD对目标host restore进程不可见仍成功；缺失/损坏必要目标对象失败，不回源 |
| 普通工作流 | 真实Runtime+Tool压力checkpoint、选节点、Runtime-first交付、Tool反应式恢复、真实工具完成 |
| 机制保留 | 同域快路径、两代incremental-cow、zstd-page、base WARM alias、lazy依赖GC；借还/LLC/QoS不丢 |
| 自助运维 | fresh shell、重复apply、非零失败、helper恢复、cleanup preview/apply；隔离fresh install另验 |

源不可见必须作用于目标**host restore/VMM进程**；guest看不到源不算。不rename/chmod旧模板制造条件，使用本任务小fixture的隔离mount view/domain allowlist和实际打开路径证据。共享DRAM是允许例外，记录其backing来源。

可扩展现有测试：`tests/test_numa_placement.py`、`test_numa_joint.py`、`test_policy_v2.py`、`test_priority_prepare.py`、`test_snapshot_pool.py`、`test_snapshot_compression.py`、`test_snapshot_scheduling.py`、`test_cube_snapshot_metadata.py`、`test_base_cache.py`、`test_host.py`、`test_host_scripts.py`，以及schema4相应测试。只补影响结果的逻辑/失败分支，不写镜像实现的无效测试。

通过的门只有相关机制更改才重跑。unit/mock、单VM库调用、standalone branching均不能替代普通worker真实迁移；已有主机恢复不能宣称clean-machine安装通过。

## 11. 最小实验矩阵和指标

### 11.1 冻结实验

先用真实 SWE-rebench corpus 的 2—4 个 case 验证依赖和迁移能力；正式实验按用户补充约束固定为 80 个 base × 16 个 rollout，共 1280 session。每个 session 仍是原 Runtime＋Tool，保留模板和工具行为。正式存储容量必须覆盖这 80 个 base；不能因空间不足缩小正式规模，也不能把小规模能力验证外推为 Full1280 性能结论。

优先复用核实过的node0 0—79/node1 80—159 CPU域或其明确子集。为形成实际压力，可预先缩小CPU/本地DRAM预算，所有arm相同并公开。共享DRAM保留且预算固定；若始终无checkpoint/move，不能声称测到迁移效果。不能写无关.cow或任意暂停Tool制造收益。

短calibration确认双SWAPPED、队列和真实迁移机会，然后冻结case→session、到达、初始放置、预算、cache inventory。不能见到结果后只改某个arm。

| 场景 | 对照 | 问题 |
|---|---|---|
| A：负载较均衡，两边已有模板 | home/load-aware/cost-aware | 是否无谓增加成本，允许结果基本相同 |
| B：原节点真实排队，目标有余量且缺失字节少 | 同上 | 节约等待是否超过迁移成本 |
| C：原节点排队，目标缺真实rootfs/冷依赖 | 同上 | cost-aware是否避免load-aware昂贵迁移，收益边界在哪里 |

**先做B三组，再C，余时A或重复B/C。** 所有对照同实现/机制，不拼不同版本。B/C的副本差异事先声明，准备时间/流量可排除于正式窗口但必须单独报告。WARM hit仍是真共享命中；可一致设小WARM预算研究冷缺失，不删除旧模板。

每arm建议全部计划投放完成后继续5分钟，总时限15分钟。若预期投放超过10分钟，启动前统一缩规模或明确统一更长上限。若现有runner没有精确截止选项，在既有CLI/supervisor补齐并记录截止事件，不另写实验runner。保留unfinished，不只统计完成快的trace。原截止前至少留30分钟整理。

真实数据路径/主机门未过就停止性能解释，不用旧proxy凑数。Full1280/2560仅在容量/时间/正确性满足后用于大并发验证，不为赶期限省前置门。

### 11.2 记录口径

每session/role/generation/事务保存monotonic timestamp和wall锚点、epoch、source/target/native handle：

```text
offered_arrival → dispatch → decision
→ resource_wait / transfer_queue → transfer_start/end → import/publish
→ restore_start/native_ready → reconnect/response_delivery
→ tool_start/end → trace_terminal → cleanup_terminal
```

从事件计算，不用重叠总数相减猜排队。报告：

- creation/checkpoint/native restore/含排队resume-to-ready的p50/p90；Tool执行均值、tools/s、完成trace/投放/失败/未完成数。
- 主吞吐从首个计划投放到共同截止；可附固定post-submission窗口，写清分母，不悄悄排除慢arm尾部。
- arrival到可执行、等待/transfer/import/decompress/reconnect；端到端只解释可比完成集合，同时给删失情况。
- 两节点平均占用逻辑核=CPU seconds/wall seconds，RSS/物理charge、WARM/base/借用占用、SSD allocated/预留峰值。
- 迁移候选/成功/拒绝、原地恢复、目标本地/共享WARM命中分别计；logical/payload/interface bytes、有效带宽、重叠流。
- 预测ready与实测及成本来源；预测不是性能结果。

每run保留spec、host plan/profile/receipt、boot ID、模板/组件哈希、资格、对象清单、qdisc/counters、events JSONL、样本、terminal rows、报告。建议本地 `.artifacts/peer-local-ssd-20261109/`、远端本任务results，均不覆旧结果/图。

## 12. 时间安排、回滚与最终交付

| 从原起点计时 | 重点 |
|---|---|
| 0—45分钟 | 主机/空间/native路径，约定接口，尽早确认destination import可行性 |
| 45—150分钟 | 并行native、transport/host、binding/账本，schema4与tracked patch同步 |
| 150—240分钟 | 真pair双向恢复、普通worker压力、失败/并发门，修影响结果错误 |
| 240—330分钟 | B三组，余时C/重复；隔离安装/自助命令验证 |
| 330—360分钟 | 汇总、清理自有VM、核对遗留资源、交付与限制 |

时间不足压缩矩阵，不降低实际消费、完整依赖、账本正确三个门；不为无关鲁棒性耗尽窗口。

```bash
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  --output-root "$PEER_RESULTS" experiment abort "$PEER_RUN_ID" \
  --cleanup-timeout-seconds 600
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment snapshot-storage cleanup "$PEER_HOST_PLAN"
# 审查preview，确认只涉及不再使用的本任务资源后：
bash scripts/clawbox --machine-env "$PEER_MACHINE_ENV" --sudo \
  experiment snapshot-storage cleanup "$PEER_HOST_PLAN" --apply
```

cleanup默认保留结果/来源/镜像内容，停止helper/卸载与删数据分开。删除只用receipt自有路径，先查引用/打开句柄；不killall、不docker prune、不递归清共享目录、不删旧模板。

回滚先核验本任务VM清理，再恢复**本轮安装前**备份的全部匹配native/SDK/配置，不退回过早24764 Shim；停止本任务namespace/helper，恢复原host plan，验证一对旧标准创建/restore。保存失败组件/日志和所有结果。重启后重新查mount/cache registry/资格，不猜WARM仍在；中断arm不能拼成连续计时。

最终交付必须回答：

1. 数据实际从哪里读，恢复是否真正换了节点？
2. 既有优化怎样保留并组合，主worker的状态/账本是否验证？
3. 什么场景迁移更快，什么时候留原节点更好，有多少实测样本？
4. creation/checkpoint/restore p50/p90、吞吐/完成数、网络与资源如何变化？
5. 新机器、已有机器、中断恢复分别用哪些公开命令，哪些环境尚未验证？

完成清单：**tracked native patch和ClawBox代码、schema/例子、guide/reference、目标真实消费与普通trace证据、至少一组匹配对照、owned资源清理、可回滚安装receipt。** 未过项如实列出；计划、编译或服务启动不能写成实验完成。
