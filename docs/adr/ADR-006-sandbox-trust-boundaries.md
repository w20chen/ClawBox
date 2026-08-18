# ADR-006: Sandbox Trust Boundaries

> Status: Proposed (M2 最高优先级)
> 日期: 2026-08-18
> 关联: 路线图 §7.1、§7.3、§1.2、§5.3

## 背景 (Background)

当前 Tool Bridge 与不可信 shell 在同一 Tool 容器/微 VM 内以 guest root 运行；恶意命令理论上可杀 bridge、改 `/testbed/.clawbox/tool-bridge.jsonl`、读 Tool 端 Secret projection、脱离 process group 建后台进程。Firecracker 保护宿主和其他 VM，但不保护同一 Tool VM 内 supervisor/audit 完整性。guest root 是 Kata mode-0000 volume 的暂时兼容形态（有真机证据）。M2 之前不得向不可信外部租户开放。

## 决策 (Decision)

1. **Tool Supervisor 保持 guest root（读 Kata Secret volume、管 cgroup），但每条任务命令以专用非 root UID/GID（sandbox UID）执行**：
   - 镜像合同固定 sandbox UID/GID；`/testbed` 只对该 UID 可写；Secret、bridge binary、audit spool 对该 UID 不可读/写。
   - Bridge 启动时读 key 并保持最小 fd；创建子进程时清理 env/fd/capability，设 non-root credential、`no_new_privs`、受审核 seccomp、独立 process group。
2. **每条 execution 在独立 cgroup v2**；超时/取消用 `cgroup.kill`；bridge 是 subreaper，执行结束后证明后台子孙进程为 0。cgroup 先用于隔离/清理，M5 才用于 ClawTune 调优。
3. **audit 写入 sandbox UID 不可改的目录**（如 `/var/lib/clawbox/audit`），monotonic sequence + execution ID + previous-record hash；不信任 `/testbed` 里的 trace 作为唯一审计证据。
4. **先跑真机 cgroup feasibility gate**：unified cgroup v2、controller 可用、supervisor 有可写受控 subtree、`cgroup.kill`/`cgroup.events populated` 生效、sandbox UID 不可写父/兄弟 cgroup。失败则 M2 阻塞。
5. **Runtime credential broker**：真实 provider key 留在 broker；Attempt 只拿短寿命、有 model/tenant/token/cost limit/expiry 的凭据。ClawTune sidecar 只绑 `127.0.0.1`；OpenClaw 子进程环境不含真实 provider key/upload token/signing key。
6. **Tool 永不获取 LLM key/中央产物读凭据/其他租户信息**；Runtime/Tool 无 hostPath、无共享 PVC/RWX、无 Kubernetes API token；无 runc/QEMU/cloud-hypervisor fallback。

## 备选方案 (Alternatives)

- **保持一切 guest root**：audit/limit 可被绕过，仅限 M0 兼容，M2 拒绝。
- **native sidecar 独立容器降权**：Kata 该宿主无法跨容器共享 volume（有证据），需重跑全部 gate，暂不采用（单容器内分权）。
- **eBPF/宿主 privileged DaemonSet 做隔离**：超范围，M5 才评估 Tool-local eBPF，拒绝现采用。

## 安全/运维后果 (Security / Ops)

- 恶意命令黑盒回归：读 key/audit、kill bridge/PID 1、fork bomb、double-fork/setsid、占满 stdout/disk/memory/PID、写 `/proc`/sysfs → 必须受限且 Cell 仍可清理。
- supervisor 位于受保护 parent cgroup，预留 headroom；workspace/`/tmp`/output/audit 独立配额。

## 迁移和回滚 (Migration & Rollback)

- 迁移：先实现 Tool Supervisor protocol core（framing/version/capability、execution ID、deadline/cancel、identity/fencing、幂等查询）；再做 non-root execution；恶意命令测试先于性能调优。
- 回滚：旧 Runtime 不允许静默连到不兼容 bridge（版本协商 fail closed）；可保留 guest-root 模式直到 M2 完成，但不对外部租户开放。
