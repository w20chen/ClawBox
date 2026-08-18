# ADR-004: Artifact Immutability

> Status: Proposed (M2 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §7.2、§6.2、§14.2

## 背景 (Background)

当前 ingester 用 SQLite + emptyDir；trace 可变文件在同一 offset 返回 409 后 uploader 跳过整文件仍可写 final marker（“假 complete”）。结果以任务名做主键不可变。M2 需要强完整、可恢复、可审计的产物系统。

## 决策 (Decision)

1. **Content-addressed blob**：trace/log/patch/final answer 作为 SHA256 content-addressed blob 写入 S3 兼容对象存储；DB 保存 Attempt-scoped manifest（digest、offset/sequence、size、media type、finalized_at）。
2. **Manifest 是完整性单位**：最终 manifest 枚举全部对象 + root digest；`receipt` 只在 result + final manifest + 所有 required blob 存在且 digest 一致时 complete。Agent 失败也应有 final manifest；缺 required 对象不得返回 durable success。
3. **修复“409 后跳过整文件”缺口**：sidecar 轮转/快照出 append-only segment spool 后上传，不直接跟踪会被 OpenClaw 重写的活动文件；manifest 无 gap 验证。
4. **上传协议版本化**：每 chunk/blob 幂等；Cell Controller 在进入 Succeeded 前用 Attempt identity 独立查询/验证中央 receipt，不只相信 Runtime Job exit 0。
5. **artifact 状态**：`partial/final/expired/quarantined`；删除是审计 tombstone + 后台 GC，绝不用用户提供的路径直接删文件。
6. **保留/配额/legal hold** 按 tenant/policy；下载用短寿命 signed URL；静态加密 + TLS + bucket policy + tenant/project 授权。
7. **审计流**：单调 sequence + hash chain/签名、WORM/object lock、时钟同步、脱敏、tenant 级访问/保留策略。

## 备选方案 (Alternatives)

- **可变文件直传**：无法保证 receipt 完整性，拒绝。
- **全部放 DB**：大 blob 拖垮 PostgreSQL，拒绝。
- **SQLite/PVC 作为最终方案**：Pod 丢失丢数据，拒绝。

## 安全/运维后果 (Security / Ops)

- 不信任不可信 VM 的“已上传”声明；中央验证是最终状态门。
- 恶意 payload/path traversal/chunk collision 有独立拒绝路径；超大 payload 受限。

## 迁移和回滚 (Migration & Rollback)

- 迁移：SQLite→PostgreSQL 双写 + 存量结果回填 manifest；S3 逐步接管。
- 回滚：停新 manifest 写、保持 receipt 语义在旧存储可读；数据不丢。
- 完成标准：上传中重启后最终 manifest 要么完整要么明确失败，绝不静默丢 trace；完成一次备份恢复演练。
