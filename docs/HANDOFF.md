# CubeSandbox migration handoff (2026-09-03)

## Goal and frozen architecture

Implement only this path:

`clawbox experiment -> Run/Attempt/outbox -> SandboxTask v1alpha2 -> one ExperimentWorker Job -> one in-process PolicyCoordinator per arm -> official cubesandbox==0.7.0 SDK -> CubeSandbox MicroVM`.

Do not add another CRD, scheduler, queue, runtime backend, node agent, or policy service. Kata, direct Firecracker, SSH, and backend selection must be deleted after the real Cube vertical slice passes. The complete user requirements are in the conversation attachments; the architecture freeze is the controlling addendum.

## Implemented locally

- Strict ExperimentSpec v2, deterministic matrix expansion, four checked-in study specs, and a one-arm vertical-slice spec.
- Official Cube SDK wrapper, lifecycle, command executor, ownership metadata, durable sandbox journal, pause/connect/kill, and metadata fallback cleanup.
- In-process policy coordinator with FIFO admission, baseline-subtracted node memory sampling, resident/snapshot policies, LRU victims, and one capacity-rejection retry.
- ExperimentWorker: sequential arms, threaded logical sessions, atomic per-arm result+marker, JSONL/JSON/CSV, cleanup barrier, and focused replay tests.
- Thin controller that creates exactly one ConfigMap and Job, uses `backoffLimit: 0` and `restartPolicy: Never`, pins Cube `distribution_scope` to the Downward API node, and projects concise status.
- Managed API/dispatcher now persist and submit ExperimentSpec v2. CRD serves v1alpha2 only.
- Cube v0.7.0 install values/script, template helper, lifecycle smoke script, and docs. Pin: tag `v0.7.0`, commit `d0081641c59822e4e5653b7462e914410b81910a`.
- Active controller/RBAC manifests and new worker image contain no Kata/Firecracker runtime selection.

Focused local gate passed:

```text
python -m compileall -q clawbox scripts
python -m pytest tests/test_controller_v2.py tests/test_cube.py tests/test_policy_v2.py tests/test_experiments_v2.py --basetemp .pytest-tmp-root
# 11 passed
python -m clawbox.cli experiment validate examples/experiments/vertical-slice.yaml
# valid, one arm
```

Managed API creation/idempotent replay was also checked ad hoc with SQLite/TestClient (201 then 200, same Run ID).

## Remote Kunpeng state

Host: `ssh weitianc@193.124.7.2` (the `kunpeng` alias was not available in this environment).

- openEuler 24.03 LTS-SP1, aarch64, kernel `6.6.0-72.0.0.76.oe2403sp1.aarch64`, Kubernetes 1.35.7, containerd 2.3.4, `/dev/kvm`, cgroup v2.
- Root now has about 1.1 TiB free. User removed LVM `clawbox/fc-pool` and wiped PV labels on `/dev/nvme0n1` and `/dev/nvme10n1`. Never use `/mnt/ann-diskann`.
- Old containerd file was moved to `/etc/containerd/20-clawbox-firecracker.toml.retired`; containerd/kubelet are active and node is Ready.
- Helm v3.17.3 is installed at `~/.local/bin/helm`.
- Source snapshot was uploaded to `~/ClawBox-cube`, but it predates the final local metric/test edits. Resync from the committed branch before deployment.
- `scripts/install-cubesandbox-kunpeng920.sh check` passed and found Service CIDR `10.96.0.0/12`, non-overlapping Cube CIDR `172.16.0.0/18`.
- Old `clawbox-benchmarks` namespace was removed after clearing 201 obsolete finalizers. `clawbox-system` was removed. No Cube installation has been attempted yet.
- The disk-full incident created thousands of stale Tigera pods: last count was ~4170 in `tigera-operator`; Calico/CoreDNS statuses were stale. Clean these controlled pods and wait for Calico/CoreDNS to be Ready before Helm install. Do not touch `/mnt/ann-diskann`.

## Must finish next

1. Resync committed source to `~/ClawBox-cube`.
2. Repair Kubernetes pod backlog (delete stale controller-owned pods in `tigera-operator`, `calico-system`, and stale non-current kube-system mirrors as appropriate; verify one healthy replica per controller and working pod networking/DNS).
3. Install Cube with fresh secrets and `PATH=$HOME/.local/bin:$PATH`; use `CUBE_USE_CN_MIRROR=1` only if pulls require it. The script creates a 200 GiB loopback XFS at `/data/cubelet`; no raw disk formatting.
4. Register a known ARM64 image/template. Confirm the helper prints the resolved image digest; improve alias reuse matching if needed.
5. Run the real `smoke-cubesandbox-kunpeng920.py` and `helm test`.
6. Build/push ARM64 control-plane and worker images to the local registry, replace manifest image tags with immutable digests (the CRD requires worker digest), apply CRD/RBAC/controller, and run the vertical slice through SandboxTask/Job/controller.
7. Run a real four-session replay and a small multi-policy matrix; save evidence/results.
8. Implement the real OpenClaw driver. **Current worker incorrectly routes `agent.driver=openclaw` through recorded-trace replay.** It must instead run trusted OpenClaw in the worker while sending repository/file/shell tools through the Cube SDK. Do not claim OpenClaw acceptance until tested.
9. Complete result fields still missing (resident concurrency/paused-time/prefetch hit-miss and reliable template/image digest discovery) and classify platform capacity separately from policy wait.
10. After the real Cube vertical slice passes, delete old Kata/direct-Firecracker/SSH modules, scripts, manifests, entry points, old tests, and multi-backend docs; rewrite README to describe only Kubernetes + CubeSandbox.
11. Rewrite/retire legacy tests. Full pytest currently fails collection because old tests import removed schema-v1 helpers (`resolve_baseline`, `validate_workflow`). Do not restore the old public backend API just to satisfy them.

## Known review points

- `Dockerfile.worker` does not yet install OpenClaw/Node because the real OpenClaw-to-Cube tool adapter remains unfinished.
- Worker paths referenced by checked-in specs work because `examples/` is copied into the image; external inputs should be placed under `/data/clawbox-inputs`.
- Controller success `resultRef` resolves to `<resultHostPath>/<runID>/summary.json`.
- Controller cancellation currently may move directly to `cleaned` after successful cleanup; dispatcher maps `cleaned + cancelled errorCategory` to cancelled.
- The chart v0.7.0 lacks values for the two research quota controls, so the installer patches the pinned source deterministically: CubeMaster CPU/memory ratios to 1 and Cubelet paused release ratio to 1.
- Do not claim a real smoke, worker integration, four-session replay, or full suite pass: none has run yet.
