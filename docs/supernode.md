# 双计算节点与共享内存池

按[逐条命令指南](self-service.md)执行 `host init → check → apply → configure → launch → report`。
默认节点为 NUMA 0、1，各有 36 GiB LOCAL，LOW/HIGH 为 28/32 GiB；NUMA 2 提供一份
128 GiB 共享池。主机必须至少有三个 NUMA 域，CPU 编号以 `host inspect` 为准。

## 配置和放置

主机 YAML 的 `compute_nodes` 分别设置节点 ID、NUMA、CPU 子集、内存容量和水位；
`warm_node/warm_gib/shared_borrow_percent` 设置唯一共享池。修改后重新 `host check/apply`，
再 `configure`。`--pool-memory-gib` 必须等于各节点 HIGH 之和；默认由成功的主机 profile 填入，
无需手工指定。总量相同不代表节点分布相同，比较实验时必须保留完整拓扑。

默认 `round_robin` 按会话序号在配置的节点列表中轮流放置，同一会话的 Runtime 和 Tool
始终选同一节点，各策略重复使用相同映射。并发 1 只运行在第一个计算节点；并发 2 覆盖两个节点。
固定映射可以作为后续 NUMA-aware 策略的对照：

```bash
clawbox experiment configure eval.yaml eval-explicit.yaml \
  --concurrency 4 --placement-policy explicit \
  --session-compute-nodes node0,node1,node1,node0
clawbox experiment describe eval-explicit.yaml
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  eval-explicit.yaml --run-id explicit-01
```

显式列表必须覆盖最大并发数，较小并发使用它的前缀。这里是静态会话放置，不会在运行中搬迁
会话或按负载自动重调度。CPU 集合是允许执行的位置，不是 vCPU 配额，也不禁止宿主机其他进程
使用这些 CPU；VM 的 vCPU 数量仍来自 Runtime/Tool 模板。

## 内存控制与科研边界

- 每个计算节点独立执行准入、LOW/HIGH 滞回、回收及 HARD 借用决策。一个节点越过 HIGH，
  只阻塞该节点的新会话；不能用另一节点的空闲容量掩盖它的压力。
- NUMA 2 上的活跃 VM 借用和 WARM 快照共享同一份全局账本。借用先预留 VM 配置容量，
  再改变其内存绑定；所有计算节点的借用之和受 `shared_borrow_percent` 限制。
- Runtime/Tool 创建及恢复后，在 Worker 分发后续工作前验证叶 cgroup 的 CPU 和内存绑定；
  借用只改变内存节点，CPU 仍在原计算节点。恢复重新绑定到原计算节点。
- LOCAL 容量是依据采样执行的控制器边界。内核 `memory.max` 约束所有 VM 的
  `LOCAL 容量之和 + 全局借用上限`，不能解释成每 NUMA 的硬配额。
- 初始启动、恢复时 Guest 自主继续运行到绑定完成之间存在短暂窗口，受父 cgroup 的
  计算节点合集约束；当前接口不提供启动前的逐 VM 绑定。`cpuset.mems` 的变更不保证已有页立即迁移。真实物理驻留由
  `memory.numa_stat` 测量。无法归属 NUMA 的内核 charge 保守计入每节点准入，
  聚合物理占用只计算一次，所以不能把各节点控制用量简单相加。
- WARM tmpfs 容量是上限，内存按实际写入消耗。其页不在 VM 父 cgroup 中；
  报告分别给出共享池活跃页、WARM 物理分配和快照账本预留，不能互相替代。
- 该模型复用真实 NUMA 距离，未仿真 UB 协议、交换网络或可调链路带宽/延迟。

## 检查结果

`launch` 的资格验证逐节点测试共享内存借还和配置的快照恢复机制；正式并发运行验证
会话放置。失败不会变成成功结果。`report` 包含每节点容量、峰值、HIGH 次数和已验证绑定数。
各 arm JSON 的 `performance.compute_nodes` 保存实际绑定记录、逐节点准入与水位数据；
`session_trace_assignment[].compute_node` 给出会话映射。

`memory-timeseries.csv` 的 `numa_N_resident_gib` 是对应 NUMA 的驻留字节，
`unattributed_gib` 是未归属的 charge，空值表示没有该项观测。
聚合表的越水位时间在多节点模式下是各节点秒数之和，不是全局墙钟持续时间；
各节点峰值出现时刻可能不同，不应相加当作同时峰值。后续 CPU 调度研究应同时保留
会话映射、CPU 集合、NUMA 驻留时间序列及原始 PMU 记录。
