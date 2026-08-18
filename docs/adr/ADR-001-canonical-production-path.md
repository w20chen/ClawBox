# ADR-001: Canonical Production Path

> Status: Proposed (M0 完成后、M1 代码前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §3.2.1、§13、§6.5

## 背景 (Background)

仓库历史上存在两套控制面：`clawbox/scheduler` + `allocator` + `controller` + `node_agent` + `tool_agent` 的旧执行面，以及 `SandboxTask CRD → CellReconciler → Tool VM + Runtime VM` 的现代生产路径。旧路径不是现代执行路径，但其中 KB overlay、lease/fencing、prediction/observation 契约、NUMA capacity 建模和幂等 API 经验值得保留。必须冻结并收敛旧路径，确保**生产只有一条可审计的写路径**。

## 决策 (Decision)

1. **活跃执行 source of truth 是 Kubernetes CR；用户与审计 source of truth 是 PostgreSQL。**
   - Managed API + PostgreSQL 唯一写权：用户身份、Run intent、幂等、cancel/retry intent、审计事件。
   - Dispatcher：从 outbox 幂等创建 CR（以 Attempt ID 为幂等键），转发单调 desired state。
   - Cell Controller：只写 CR status/conditions 以及其拥有的子资源/Reservation。
   - Status Projector：只追加 DB event / 更新可重建投影，绝不反向改 CR spec/status。
   - Artifact/KB services：只写各自 manifest/observation/snapshot。
2. **唯一创建 execution CR 的入口是 Dispatcher；唯一创建 Tool/Runtime 工作负载的是 Cell Controller。**
3. **旧控制面收敛**：冻结新功能 → deprecation header + 下线日期 → 将 KB/lease/fencing 不变量提取到新 package（M4 `clawbox/tuning`、M3 `CellReservation`）→ 无外部消费者时导出历史为只读归档并删除旧执行面；有消费者时建 compatibility adapter（只把请求转成新 AgentRun，无 Pod/Lease/Secret 写权限）。
4. **benchmark launcher 变成 API client/验收工具**；生产不保留直接 CR 写路径。break-glass 也必须走有 JIT/MFA/审批、同样策略和全量审计的特权 API。直接 CR 只允许在编译/部署隔离的 dev/灾难诊断 namespace，生产 RBAC 显式拒绝。
5. **字段所有权表**（任何组件不跨界修改）见路线图 §6.3，作为本 ADR 的附录。

## 备选方案 (Alternatives)

- **只保留 CRD 路径、不引入 PostgreSQL**：无法提供租户/审计/幂等/retry 的强一致语义，M1/M7 的 API 面无法满足，拒绝。
- **API 与 controller 都直接写业务状态**：双写竞争、无法仲裁，拒绝。
- **保留旧 scheduler 作为第二路径**：两条写路径无法保证安全/审计，拒绝。

## 安全/运维后果 (Security / Ops)

- Managed API 没有任何 Pod/Secret 创建权限 → 消除 confused-deputy（租户不能指定任意 Secret 名）。
- 任一组件重启不丢请求、不重复创建可执行 Attempt（outbox + 幂等键）。
- 审计事件单调有序、可回放；Break-glass 全量审计。

## 迁移和回滚 (Migration & Rollback)

- 迁移：outbox 从新 API 首次写入；把现有 terminal CR 回填为历史 Run/Attempt（generation+provenance）；dispatcher 逐步接管创建。
- 回滚：停 dispatcher → CR 路径回到 dev-only 直写（不面向租户）；PostgreSQL 写停在 rollback 点，CR 状态不丢失。
- 收敛完成定义：只有 Managed API/Dispatcher 能创建 execution CR，只有 Cell Controller 能创建工作负载；仓库不存在两套活跃 reservation/状态机/owner。
