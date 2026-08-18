# ADR-008: Observation / KB / Tuning Modes

> Status: Proposed (M4 前必须批准)
> 日期: 2026-08-18
> 关联: 路线图 §9.3、§9.4、§9.5、§10.4、§0.1

## 背景 (Background)

当前只有 OpenClaw hook trace + Tool Bridge 末尾 JSONL 的旁路观测；ClawTune 是 observe-only/hook-only，cgroup/affinity/NUMA 显式关闭。没有签名、去重、质量门、tenant 隔离、generation 或影子预测。不能通过把开关改成 true 宣称闭环。

## 决策 (Decision)

1. **Observation 是不可变、可签名、可按 tenant 删除/重建派生数据的正式 artifact**，不是从日志 grep：
   - 唯一键 `(tenant, attempt, execution, observation_type, schema_version)`；同 digest 重传幂等，不同 digest 冲突进 quarantine/audit，不训练。
   - repo fingerprint/tool name/command feature/digest 定义脱敏与 tenant-scoped 规则；默认不上传 argv/env/文件内容原文。
   - `complete=false`、collector degraded、身份/签名错误、schema 未知、execution join 失败 → diagnostic store，不进 active KB。
   - timeout/OOM/failure 是 censored observation，只进 diagnostic/censored store；只有明确支持 censoring 且经批准的 estimator 才可消费；第一版 active estimator 只用 complete/trusted 成功样本。
2. **Central Observation Projector + KB Store**：signed raw → immutable store → schema/identity/signature/quality validator → trusted_observations（append-only）→ tenant KB builder → immutable KB snapshot（generation + provenance）→ Prediction API（shadow only）。
3. **tenant overlay**：public baseline、tenant overlay、repo/tool layer 来源/授权/隔离明确；一个 tenant 的 private sample 不数值混入另一 tenant；删除 tenant 后 derived snapshot 可重建。
4. **KB snapshot 含**：generation、schema/model/algorithm version、input observation range/digest、created_at、builder revision、quality summary；可回滚、可离线重放、可按 tenant 删除重建。
5. **执行分层**：`observe → shadow → advise → canary-enforce → active-enforce`，每层可在全局/tenant/template 三维一键降级到固定 profile；模式由服务端 Policy 选择，不由 prompt/Agent 改写；全局/tenant/template/run 级 kill switch。
6. **Shadow prediction 不改真实资源**：`FixedProfileSizer` 仍决定真实 Cell 大小；prediction 只写 status/event/指标；任务后对比 prediction vs actual，统计 underprediction/OOM risk、over-allocation、匹配覆盖率、校准、drift；离线 replay dataset 分层，train/eval 分离。
7. 从旧 `clawbox/scheduler/kb.py` 提取 `RuntimeToolResourceKB` 经验到新 `clawbox/tuning`，生产路径不调用旧 `Scheduler.run()`。

## 备选方案 (Alternatives)

- **全部样本都训练**：投毒/跨 tenant/低质量污染 KB，拒绝。
- **无签名/无质量门**：无法追溯与回滚，拒绝。
- **直接 active-enforce**：未过 canary 门槛，拒绝。

## 安全/运维后果 (Security / Ops)

- 观察伪造、跨 tenant 注入、低质量/未签名样本被 quarantine；generation 可审计回滚。
- 任一 KB/预测故障自动回退固定安全 profile，不拒绝本可运行任务也不自动放大资源。

## 迁移和回滚 (Migration & Rollback)

- 迁移：先建 observation schema/签名/validator → 中央 projector → tenant KB → shadow prediction。
- 回滚：停 projector/prediction 不影响真实执行（固定 profile 兜底）；derived snapshot 可从 raw observations 重建。
