# ADR-007: Tool Execution Protocol

> Status: Proposed (M2 core 先行，M4 补齐完整 envelope)
> 日期: 2026-08-18
> 关联: 路线图 §9.1、§7.1.7、§10.6

## 背景 (Background)

当前 SSH `exec` 只发一段 shell 字符串，execution ID 由 Tool Bridge 内部随机生成，不能确定性关联 ClawTune span、资源预测和中央 observation。M2 建立协议运输/身份/生命周期 core；M4 补齐 correlation/observation 语义。

## 决策 (Decision)

1. **继续用 SSH 作运输，但使用版本化 subsystem + 长度前缀 envelope，不把 JSON 拼成 shell 字符串。**
2. **请求 envelope 至少包含**：`schema_version, tenant_id, project_id, run_id, attempt_id, sandbox_uid, execution_id, traceparent, tool_name, command_digest, argv_or_command, cwd, sanitized_env, deadline, output_limit, resource_intent, policy_version, prediction_id, kb_generation, nonce, issued_at, expires_at, fencing_token, signature`。
3. **响应/observation envelope 至少包含**：`execution_id, started_at, completed_at, duration, exit_code, signal, timeout, cancelled, oom, stdout_bytes, stderr_bytes, truncated, cpu, memory, io, pids, network/process summaries, requested_limits, applied_limits, collector_type/version, collection_quality, observation_digest, supervisor_signature`。
4. **Tool Bridge 拒绝**：过期、nonce 重放、tenant/attempt/sandbox UID 不符、command digest 不符、fencing token 过时、超出 Cell 硬上限的请求。
5. **execution ID 在 Runtime/ClawTune 侧生成**，贯穿 plugin span、Tool Bridge、observation、artifact、日志。命令已在 Tool 端开始后，运输失联不得盲目重放；先按 execution ID 查询或终止。
6. **握手返回 bridge build revision + schema/capability 列表**；Runtime/Tool 不兼容时在执行用户命令前 fail closed。任务镜像 mapping/attestation 记录 protocol/capability，避免新 Runtime 连旧 bridge 静默降级。
7. **取代脆弱 OpenClaw bundle 字符串补丁**（§9.2）：优先上游 SSH sandbox root 能力或 ClawBox backend；上游可用前固定 upstream revision 的可审查 patch + CI 契约（compile/start OpenClaw + read/write/exec 契约），`clawtune.lock`/integration lock 锁定版本。

## 备选方案 (Alternatives)

- **保持裸 shell 字符串**：无法确定性 join、防重放/篡改，拒绝。
- **换 HTTP/gRPC 运输**：暴露更大网络面，且 Runtime/Tool 现链路已通；SSH + 版本化 subsystem 足够，暂不换。
- **直接 patch bundle 作为长期方案**：升级静默失效/清空 `/testbed` 风险，拒绝。

## 安全/运维后果 (Security / Ops)

- 重放/篡改/跨 tenant/版本不兼容均有拒绝路径；执行有 fencing 与幂等查询。
- 协议版本在镜像 attestation 中，可审计、可回退。

## 迁移和回滚 (Migration & Rollback)

- 迁移：M2 先实现 core（运输/身份/生命周期），M4 加完整 correlation/observation 字段；bridge 与 runtime 同步升级。
- 回滚：版本协商 fail closed；旧组合不再被准入。
