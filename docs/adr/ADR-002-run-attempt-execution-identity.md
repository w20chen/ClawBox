# ADR-002: Run / Attempt / Execution Identity

> Status: Proposed (M1 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §2.1、§6.2、§6.3

## 背景 (Background)

当前 ingester result 以任务名为主键且不可变，重用名字会 409；用户可见 Run 与内部执行 Attempt 未分离，retry 语义不完整。M1 需要服务器生成的全局身份、幂等、retry 和重放语义。

## 决策 (Decision)

1. **三层身份，全部服务器生成：**
   - `Run ID`: ULID（有序、可索引）。
   - `Attempt ID`: ULID。一次不可变执行尝试，对应一个内部 `SandboxTask`。
   - `Execution ID`: UUID（Tool Protocol 的每次命令执行，贯穿 plugin span / Tool Bridge / observation / artifact / 日志）。
2. **幂等**：`POST /v1/runs` 必须带 `Idempotency-Key`；`(tenant_id, idempotency_key)` 唯一；同 key + 同 request digest 返回既有 Run；同 key + 不同 digest 返回 409。
3. **Retry**：`POST /v1/runs/{run_id}:retry` 创建新 Attempt ID 和新 ingester/artifact namespace，绝不复用旧 result 主键；旧 artifact 保持不变。
4. **重放语义**：一个命令已在 Tool 端开始后，运输失联不得盲目重放；必须先按 execution ID 查询或终止（query-before-replay）。
5. **名字只做展示**：任务名/prefix 不再作为任何持久主键。
6. 状态机（Run/Attempt/Cleanup）按路线图 §6.2 固化；`platformOutcome` / `agentOutcome` / `artifactOutcome` / `evaluationOutcome` 分离（§14.5）。

## 备选方案 (Alternatives)

- **任务名作为 result 主键**：retry/tenant 冲突 409，拒绝。
- **客户端自声明 ID**：无法保证全局唯一与防篡改，拒绝。
- **单层 Run 即 Attempt**：无法表达 retry/attempt 生命周期，拒绝。

## 安全/运维后果 (Security / Ops)

- ID 进入全链路 correlation；任何日志/事件/artifact 可用 Run/Attempt/Execution 三级关联。
- 租户不能越权读取其他 tenant/project 的 Run/event/artifact（authz 由服务端从 OIDC subject 导出 tenant/project，不接受自声明）。

## 迁移和回滚 (Migration & Rollback)

- 迁移：历史 task_id 导入时生成 legacy run/attempt ID + provenance；ingester 兼容旧 key 的只读查询；新 Attempt 全走新命名空间。
- 回滚：API 停用 retry/cancel 语义可降级到 v1alpha1 直写（dev-only）；已入库 Run/Attempt 投影可重建。
