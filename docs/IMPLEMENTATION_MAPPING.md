# Firecracker-first implementation map

> **Current-shape notice (2026-08-18):** the Tool Pod and the Runtime Job are both
> **single containers** — the Tool Bridge is baked into the task image and
> ClawTune runs as an **in-process background process** started by
> `runtime-entrypoint` inside the runtime container (Kata on the validated host
> cannot share volumes across containers). See
> [`AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md`](AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md)
> for the authoritative description.

| Objective | Authoritative implementation | Verification |
|---|---|---|
| Kata/Firecracker artifact audit | `scripts/audit-kata-firecracker-arm64.sh` | ARM64 ELF/version/config/kernel/block-rootfs/shim gates |
| Reproducible pinned assembly | `scripts/build-kata-firecracker-arm64.sh` | publisher SHA256 plus FC-0 audit |
| Production LVM thin pool | `scripts/setup-devmapper-openeuler-arm64.sh` | explicit two-disk confirmation, containerd plugin/status checks |
| Host bootstrap | `scripts/bootstrap-openeuler-arm64.sh` | plan/apply/status, backups, final stage-0 proof |
| Handler and RuntimeClass | `deploy/containerd-firecracker.toml`, `deploy/runtimeclass-firecracker.yaml` | `deploy/check-host.sh` |
| Live isolation smoke | `scripts/arm64-kata-smoke.sh` | two boot IDs, Service/NetworkPolicy, host FC process, snapshot cleanup |
| ARM64 SWE image factory | `clawbox/images/arm64.py`, `clawbox/images/swerebench.py` | native daemon, contract test, registry manifest/digest mapping |
| Static Tool Bridge | `toolbridge/main.go`, `docker/Dockerfile.tool-bridge` | ARM64 self-test, SSH key auth, bounded execution audit |
| Dual-Pod task API | `deploy/sandboxtask-crd.yaml` | schema requires immutable digest and network boundary |
| Idempotent Cell lifecycle | `clawbox/cell/controller.py`, `clawbox/cell/manifests.py` | owner references, readiness ordering, finalizer cleanup |
| In-process ClawTune sidecar | `scripts/clawtune-sidecar-entrypoint.sh`, `scripts/runtime-entrypoint.sh`, `docker/Dockerfile.runtime` | background process inside the single runtime container; observe/hook-only, fail-open, cgroup/affinity/NUMA off |
| Central artifact archive | `clawbox/ingester`, `scripts/artifact-uploader.py` | task HMAC token, chunk checksum/idempotence, final receipt handshake |
| Capacity/admission | `clawbox/cell/capacity.py`, `scripts/collect-node-capacity.py` | full-Cell atomic reservation and resource profiles |
| Benchmark submission | `clawbox/benchmark/kubernetes.py` | mapping-only SandboxTask launcher; no image fallback |
| Incremental scale | `scripts/scale-swe-rebench.sh` | steps 1/2/4/8/16/32 with thin-pool stop gate |

Target hardware evidence is operational output, not source code. A release is production-accepted only after the runbook's FC-0 through FC-5 gates and at least the required scale steps have been archived for the exact host and repository revision.
