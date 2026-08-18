# ADR-005: Image Trust

> Status: Proposed (M2 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §7.5、§2.2、§14.4

## 背景 (Background)

当前生产 manifest 仍使用 `127.0.0.1:5000/clawbox/*:dev` tag + `imagePullPolicy: Always`；registry 是单节点 loopback；无签名/SBOM/provenance/admission。M2 供应链门禁要求不可变 digest + 信任链。

## 决策 (Decision)

1. **生产 manifest 全部用 immutable registry digest**，禁止 `:dev`/`:latest`。
2. **发布镜像产生并验证 provenance/SBOM/signature**，记录 source revision、builder identity、platform、ClawTune/OpenClaw/tool-bridge revision。
3. **Admission policy（创建 VM 前终止）**：验证 `linux/arm64`、签名者、漏洞阈值、任务镜像合同（`/testbed`、sandbox UID、tool bridge protocol version）；验证失败 fail closed。
4. **Task image attestation/mapping**：记录 Tool Bridge protocol + collector capability，避免新 Runtime 连旧 bridge 静默降级。
5. **Registry 不再是单节点 loopback 的最终方案**：可用性、GC、备份、digest 保留有独立 runbook。
6. **Release gate**：候选镜像在门禁前固定 digest，门禁后只提升同 digest，不重建“内容应该相同”的镜像（§14.4）。

## 备选方案 (Alternatives)

- **tag-based 部署**：tag 可变、供应链不可审计，拒绝。
- **不做 admission**：无法拦截非 ARM64/未签名/协议不兼容镜像，拒绝。
- **持续用 loopback registry**：单点、无 GC/备份，仅限开发，拒绝作为生产。

## 安全/运维后果 (Security / Ops)

- 拒绝 tag、非 ARM64、未签名、证明缺失、合同测试失败的镜像；在 VM 创建前终止。
- 镜像锁定 digest 后，`imagePullPolicy` 不再依赖 Always 语义防漂移。

## 迁移和回滚 (Migration & Rollback)

- 迁移：先把当前三镜像 + 任务镜像固定到 digest 并签名；再上 admission；再迁移 registry。
- 回滚：admission 可先降级为 warn/audit 再 fail-closed；digest 锁定的镜像随时可回退部署（同 digest）。
