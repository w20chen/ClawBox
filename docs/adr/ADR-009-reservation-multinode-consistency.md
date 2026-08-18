# ADR-009: Reservation and Multi-Node Consistency

> Status: Proposed (M3 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §8.2、§8.1、§8.3、§3.2.7

## 背景 (Background)

当前 controller 单副本、2s 全量轮询、只支持恰好 1 个 Firecracker-ready node，通过枚举 CR status 在内存重建预留。不能直接 `replicas: 2` 或把 `build_reconciler()` 的“单 ready ARM64 node”假设删掉后上多节点。M0 真机发现 scale32 的 kata shim FD 上限（≈19 合格并发）说明容量证据必须纳入 admission。

## 决策 (Decision)

1. **引入持久 `CellReservation` CR**（或等价持久对象）：绑定 Attempt UID、node、CPU/memory/storage/pods、expiry，带 fencing token。
2. **admission 用乐观并发/resourceVersion（CAS）或专用调度单写者**，保证两个 controller 不会同时超售。
3. **NodeCapacity 是节点级、带采集时间和有效期的**：devmapper Data/Meta 预算、RuntimeClass overhead、非 Cell Pod request、Pod 数、安全余量都纳入；过期容量证据必须停止新 admission。M0 的 FD 上限类证据作为“每节点合格 Cell 数”上限参与 admission。
4. **reservation 只在 Tool/Runtime 已消失且 node-side sandbox 已确认清理后释放**，不在 CR 刚进终态时提前释放。
5. **Controller manager 改造**：watch/informer + rate-limited workqueue 替代 2s 全量轮询；bookmark/resourceVersion 过期后 relist；指数退避 + 有上限 dead-letter/condition；Kubernetes Lease leader election（先允许多副本待命，只有一个 active reconciler）；status patch 处理 resourceVersion 冲突并重试；状态转移幂等，不依赖进程内记忆；子资源除 owner UID 外记录 immutable spec hash/controller revision，同 owner 不同 spec fail closed 不静默 adopt。
6. **Orphan reaper 两步**：只读对账（CR/Pod/Job、containerd sandbox、kata shim、Firecracker PID/CID、devmapper snapshot）→ 生成 orphan report → 只清除能证明不属于活跃 Attempt 的对象；绝不做不可证明安全的广泛 `pkill`。节点出现 orphan / thin-pool 超门 / Kata gate 失败 / 无法上传证据时，移除 `firecracker-ready`/taint，停止新任务。
7. **节点维护**：cordon → 等待/取消 Cell → 对账 → 重启 → FC gate → uncordon（脚本 + runbook）。

## 备选方案 (Alternatives)

- **保留内存 reservation + replicas=2**：双 controller 无 CAS 会超售，拒绝。
- **全局聚合容量再分配整 Cell**：可能把 Cell 放不下单节点，拒绝；必须 per-node 向量 + 整 Cell 单节点放置。
- **恢复 init/native sidecar 前先做多节点**：需重跑全部 gate，且与容量无关，先完成本 ADR 的一致性基础。

## 安全/运维后果 (Security / Ops)

- 任意时刻预留总和不超过带安全余量的节点容量；controller 重启/切主后账本与活跃 VM 对齐。
- leader 失联后新 leader 在 lease timeout 内接管，不发生双 admission。

## 迁移和回滚 (Migration & Rollback)

- 迁移：先加 Reservation CR 与 status reservation 并存，验证单节点对账一致 → 切到 Reservation 作为唯一账本 → 再开多副本/多节点。
- 回滚：停 leader election 回到单副本；Reservation 可从 CR status 重建。
- M3 完成定义：单节点 2 副本只有 leader 执行；故障注入矩阵（§8.4）全自动通过；100 个混合 attempt 后 orphan=0。
