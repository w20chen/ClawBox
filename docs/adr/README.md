# Architecture Decision Records (ADR)

> 日期: 2026-08-18 · 依据路线图 §18（M1 代码前必须批准）
> 硬主线: `M0 → ADR → M1 → M2 → M3`

## 决策日志

| ADR | 决策主题 | 一句话决策 | 状态 |
|---|---|---|---|
| [001](ADR-001-canonical-production-path.md) | Canonical production path | CR 是活跃执行 source of truth，PostgreSQL 是用户/审计 source of truth；只有 Managed API/Dispatcher 创建 CR，Cell Controller 创建工作负载；旧控制面收敛 | Proposed |
| [002](ADR-002-run-attempt-execution-identity.md) | Run/Attempt/Execution identity | 三层服务器生成 ID（Run ULID / Attempt ULID / Execution UUID）；`(tenant, idempotency_key)` 幂等；retry 建新 Attempt 不复用 result 主键 | Proposed |
| [003](ADR-003-crd-versioning-cancellation-status.md) | CRD versioning/cancellation/status | 新增 v1alpha2；execution 不可变 CEL + desiredState 单向取消 CEL；Conditions 化；storage version 切换前验证 conversion/回滚 | Proposed |
| [004](ADR-004-artifact-immutability.md) | Artifact immutability | content-addressed blob + attempt-scoped manifest + root digest；receipt 仅当全部 required blob 一致；修复“409 跳文件假 complete” | Proposed |
| [005](ADR-005-image-trust.md) | Image trust | 生产全 digest 锁；signature/SBOM/provenance + admission 在 VM 创建前 fail-closed；registry 不依赖单节点 loopback | Proposed |
| [006](ADR-006-sandbox-trust-boundaries.md) | Sandbox trust boundaries | Tool Supervisor guest-root 但命令以 sandbox UID 执行；独立 cgroup + `cgroup.kill`；Runtime credential broker；先跑真机 cgroup gate | Proposed |
| [007](ADR-007-tool-execution-protocol.md) | Tool execution protocol | SSH 版本化 subsystem envelope（非 shell 字符串）；execution ID 由 Runtime 生成；签名/fencing/防重放；握手 fail closed | Proposed |
| [008](ADR-008-observation-kb-tuning-modes.md) | Observation/KB/tuning modes | 签名不可变 observation + 质量门 + tenant overlay + generation；observe→shadow→canary→active 分层，shadow 不改真实资源 | Proposed |
| [009](ADR-009-reservation-multinode-consistency.md) | Reservation & multi-node | 持久 CellReservation + CAS + leader election；per-node capacity 向量；reaper 两步审计清理；M0 FD 上限纳入每节点合格 Cell 数 | Proposed |
| [010](ADR-010-persistence-tenancy-disaster-recovery.md) | Persistence, tenancy, DR | PostgreSQL + S3 + Alembic；WAL/对象锁/KMS/审计 WORM；RPO/RTO 目标；恢复演练是验收 | Proposed |

## 使用说明

- 每个 ADR 必须包含背景/决策/备选方案/安全运维后果/迁移回滚，未满足不可视为批准。
- 批准方式：状态从 `Proposed` → `Accepted`（评审 + 记录）→ 代码实现必须与 Accepted ADR 一致；冲突时先改 ADR 再改代码，不允许代码静默分叉。
- 撤销/修订：新增修订版本（`ADR-00X-rN`），保留历史。
- 完成每个里程碑（M1/M2/...）时回查相关 ADR 是否仍与实际实现一致。
