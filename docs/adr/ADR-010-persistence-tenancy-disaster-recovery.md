# ADR-010: Persistence, Tenancy, and Disaster Recovery

> Status: Proposed (M2/M3 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §6.3、§7.2、§12.4

## 背景 (Background)

当前 ingester 默认 SQLite + emptyDir，Pod 丢失丢结果；无对象存储、无备份恢复、无保留策略；租户/审计语义缺失。M2 把产物切到 PostgreSQL + S3；M7 需要可运营的备份/DR。

## 决策 (Decision)

1. **PostgreSQL 最少表**：`tenants`、`projects`、`templates`、`template_revisions`、`runs`、`attempts`、`run_events`、`idempotency_keys`、`outbox`、`audit_events`。生产 schema 用 Alembic migration，不用启动时 `create_all`。
2. **blob 进 S3 兼容对象存储**（content-addressed + manifest，见 ADR-004）；DB 只存 manifest/状态/审计/幂等键。
3. **备份/恢复**：
   - PostgreSQL WAL 持续归档 + 定期全备 + PITR；
   - 对象存储 versioning + 跨故障域复制 + object lock；
   - etcd 快照；registry digest/signature/provenance 可在灾备站重拉；
   - KB derived snapshot 可从 immutable observations 重建；
   - 密钥/KMS 有轮换、备份、撤销、恢复程序。
4. **审计**：单调 sequence + hash chain 或签名、WORM/object lock、时钟同步、脱敏、tenant 级访问/保留策略；operator/break-glass 操作入审计流。
5. **租户隔离与删除**：所有路径带 tenant/project 授权；删除是审计 tombstone + 后台 GC；运行中微 VM 不承诺内存级灾备（worker 永久丢失 → Attempt `Interrupted/PlatformFailed`，按策略新建 Attempt，不伪装续跑）。
6. **RPO/RTO 初始目标（ADR 批准）**：

   | 数据/能力 | RPO | RTO |
   |---|---:|---:|
   | receipt 已确认 artifact | 0 | ≤4h |
   | Run/Attempt/控制元数据 | ≤5min | ≤1h |
   | 原始 KB observation | ≤5min | ≤4h |
   | KB derived snapshot | 可从 raw 重建 | ≤4h |
   | 关键审计 | ≤1min | ≤4h |
   | 运行中 sandbox | 不做内存恢复 | 按 Run policy |
7. **恢复演练是验收**：“备份 job 成功”不是；必须在隔离环境恢复，用 manifest/hash + 应用级 Run/artifact/KB 查询校验；上线前全量恢复，初期每月、稳定后每季。

## 备选方案 (Alternatives)

- **SQLite/PVC 作为生产**：无多副本、无 PITR，拒绝。
- **无对象存储全放 PG**：大 blob 不可扩展，拒绝（见 ADR-004）。
- **不做 WORM 审计**：无法满足合规/审计要求，拒绝。

## 安全/运维后果 (Security / Ops)

- 静态加密、TLS、bucket policy、tenant/project 授权、短寿命 signed URL。
- 数据删除可审计、可追溯；租户删除后 derived 数据可重建或按策略清除。

## 迁移和回滚 (Migration & Rollback)

- 迁移：SQLite→PG 双写、S3 接管 blob、Alembic expand/contract（至少保持一个发布窗口前后版本兼容）、feature flag 切写。
- 回滚：停 PG 写、回到 CR/status 重建投影（dev-only）；已写 blob 不丢。
- M7 GA 门：72h soak、故障矩阵、多租户攻击、最近一次备份恢复与升级/回滚演练在有效期内通过。
