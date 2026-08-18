# ADR-003: CRD Versioning, Cancellation, and Status

> Status: Proposed (M1 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §6.4、§3.2.3、§6.2

## 背景 (Background)

`v1alpha1` 已在真机跑通并被基准工具使用；但 `llmSecretName` 允许引用任意 Secret（confused-deputy）、`status` 只有单字段 phase/outcome、无 desiredState、无 Conditions、无不可变 execution CEL。不能破坏现有 `v1alpha1`。

## 决策 (Decision)

1. **新建 served 版本 `v1alpha2`**（语义稳定后升 `v1beta1`）；`v1alpha1` 继续 served。
2. **内部 spec 至少包含**：`runRef`/`attemptId`/`tenantRef`/`projectRef`（服务器生成）；`templateRef` + 不可变 revision/digest；`inputRef` + SHA256（迁移期兼容 inline problemStatement）；`modelProfileRef`（禁止客户端指定原始 Secret 名）；`networkPolicyRef`/`artifactPolicyRef`/`resourcePolicyRef`；`deadlineSeconds`/`commandBudget`/`maxOutputBytes`；独立的 `control.desiredState`。
3. **CEL 门**：
   - execution payload 不可变：`self == oldSelf` 只作用于 execution 字段；
   - 另一条 CEL 只允许 `desiredState` 保持不变或 `Running → Cancelled` 单向转移；禁止取消回滚或修改执行输入。
4. **status**：`attemptId`/`nodeName`/`reservationRef`/`artifactManifestRef`/`conditions[]` + 每段时间戳。
5. **Conditions**：`Accepted`、`Scheduled`、`ToolReady`、`AgentComplete`、`ArtifactsDurable`、`CleanupComplete`。清理是正交 Condition，不覆盖业务终态；当前 `Cleaned + outcome` 只是 v1alpha1 兼容语义。
6. **存储版本切换**：先实现并验证 conversion webhook、storedVersions migration、活动任务 fail-closed plan 和回滚，才切 storage version。未经验证不得仅“准备”测试。

## 备选方案 (Alternatives)

- **直接改 v1alpha1**：破坏既有 CR/工具，拒绝。
- **不加 CEL/desiredState**：取消语义不可审计、执行输入可被篡改，拒绝。
- **用新 CRD 代替 SandboxTask**：与既有一致性路径割裂，迁移成本高，暂不采用（先版本化演进）。

## 安全/运维后果 (Security / Ops)

- 租户不能通过 CR 引用任意 Secret/CIDR/Pod spec；取消只能单向，不能回滚执行输入。
- 状态/条件长期保留 outcome 与 artifact 引用，不短暂进入 Succeeded 后立即只剩 Cleaned。

## 迁移和回滚 (Migration & Rollback)

- 迁移：v1alpha2 served → conversion webhook 双向 → 新写走 v1alpha2 → 存量回填 → 验证后切 storage version → v1alpha1 退役（shed）。
- 回滚：保留 v1alpha1 served + storage；任何迁移失败回到 v1alpha1 直写路径（dev-only）。
