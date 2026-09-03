# ClawBox + ClawTune + CubeSandbox handoff (2026-09-03)

This is the complete continuation point. Read it before changing the working tree or Kunpeng deployment.

## Final architecture decision

The newest user instruction overrides the older pasted specification that said one VM per agent. Each logical Agent uses exactly two CubeSandbox MicroVMs:

1. **Runtime VM**: OpenClaw, native ClawTune plugin/tracer, model access, reasoning state, and only an authenticated `cube_shell` tool.
2. **Tool VM**: `/workspace`, repository and command execution, cgroup/eBPF telemetry, and no model credential.

The ExperimentWorker owns both VM lifecycles, runs the authenticated bridge, applies ClawBox policy to the Tool VM, joins observations by execution/session identity, and writes full/redacted traces, action and lifecycle timings, resources, cgroup/eBPF data, policy events, provenance, and cleanup evidence.

## Repository and safety

- Local tree: `C:\Users\user\Desktop\ClawBox`
- Paired routing baseline: `aeb8109 Complete paired CubeSandbox agent routing`
- The Tool telemetry/Runtime predictor milestone follows that baseline.
- Clean remote copy: `/home/weitianc/ClawBox-cube`
- Do not overwrite `/home/weitianc/ClawBox`; it is an older dirty checkout.
- ARM64 host: `ssh weitianc@193.124.7.2` (session startup takes about 60 seconds)
- Do not apply the optional BoostKit kernel patch or reboot without explicit permission.

## Previously verified baseline

- CubeSandbox `v0.7.0`, commit `d0081641c59822e4e5653b7462e914410b81910a`
- SDK `cubesandbox==0.7.0`, command dependency `e2b==2.29.5`
- Old template: `tpl-c7212cdc724844639aa65486`
- Old image: `sha256:e1cb43e12ba70b8453b45f0c063306faab8a6974aa3fd76982dc4d019d07c60d`
- Controller: `sha256:8696240154448a63cfb3d7f42aac20299fa733b9cef1352482fb82fd09ba6787`
- Old Worker: `sha256:9cdb4634a14b962e0c1e8214cf8c97d3fb356c40652258f645aedf810861a9d2`
- Real lifecycle/pause-resume, four-agent matrix, Kubernetes vertical slice, cancellation, controller restart, and cleanup previously passed.
- Evidence: `/data/clawbox-results/` and `/home/weitianc/clawbox-results/real-matrix-1`.
- Baseline full suite previously passed: 164 passed, 5 skipped.

## Paired images and READY templates

Runtime VM:

- image `127.0.0.1:5000/clawbox/runtime-cube-arm64:openclaw-clawtune-v1`
- digest `sha256:02ae0ff35f57352c615746f17734fcc425d9fac5cffa8b9d075416c5ad91523e`
- template `tpl-743b4bf146c642328ebe4e70`, alias `clawbox-runtime-arm64-2g-v3`
- OpenClaw `2026.7.1-2`, native ClawTune plugin, and `cube-tool`

Tool VM:

- image `127.0.0.1:5000/clawbox/tool-cube-arm64:clawtune-v1`
- digest `sha256:efe9a11299398c6755188e3e3a1b0c9c6067f17408e70cf39c5872284bc754a2`
- template `tpl-69e6945ec7844b26976bf4b7`, alias `clawbox-tool-arm64-4g-v1`
- workspace, `tool-bridge`, and ClawTune guest collector sources

Templates use `http://193.124.7.2:5001/...`. The read-only registry mirror is `clawbox-registry-cube`; after a reboot run `ssh weitianc@193.124.7.2 'docker start clawbox-registry-cube || true'`.

## Implemented but not deployed end to end

- Specs/arms require separate `runtime` and `sandbox` (Tool) definitions.
- `worker.py` creates/cleans two owned lifecycles, reserves combined memory, applies policy to Tool, records role/timings, and runs OpenClaw through Runtime.
- `CubeSandboxLifecycle` supports creation environment variables.
- `openclaw_driver.py` runs OpenClaw inside Runtime, starts the loopback-only native ClawTune predictor/model proxy, loads cold-start or control-plane KB snapshots, enables `cube-tool` and native ClawTune, denies host tools, injects model credentials only into Runtime, and copies Runtime traces/prediction artifacts.
- `openclaw-plugins/cube-tool/` implements `cube_shell`.
- Every Worker-issued Tool command uses the Cube command API to invoke the instrumented one-shot runner. The Tool entrypoint alone starts the eBPF guest collector; the runner gates execution on a per-command cgroup and returns cgroup/eBPF artifacts or an explicit unavailable reason.
- `clawtune_trace.py` writes exact-ID v6 spans, bridge records, and measured Tool artifacts without fabricating missing values.
- Runtime/Tool Dockerfiles and Runtime entrypoint exist.
- CRD immutability includes credential secret and timeout; Worker Pod receives `CLAWBOX_BRIDGE_HOST`.
- Examples contain separate Runtime and Tool templates; registration supports repeated exposed/probe ports.
- Runtime-to-Worker requests now carry the OpenClaw tool-call ID into the
  authoritative bridge record, and Runtime creation adds a narrow CubeSandbox
  `network.allow_out` entry for only the Worker Pod IP.

Key files: `clawbox/experiments/worker.py`, `openclaw_driver.py`, `clawtune_trace.py`, `openclaw-plugins/cube-tool/index.js`, both Cube Dockerfiles, `scripts/smoke-cubesandbox-agent-pair.py`, and `examples/experiments/openclaw-cube.yaml`.

## Current verification

After the telemetry/predictor refactor the complete local gate passes:

```text
python -m compileall -q clawbox
python -m pytest -q
# pass; 5 environment-dependent tests skipped
```

Go 1.25 successfully cross-compiles the Tool bridge tests for Linux/ARM64. A
native Kunpeng run exposed the host cgroup hang described below; the collector
was also hardened to avoid unnecessary remounts on an already-rw cgroup tree.

## Storage recovery completed

After reboot, `no more resource` actually meant `/var/run/s3lvol.sock` was missing. The Helm chart does not install the host daemon. `scripts/recover-cubesandbox-s3lvol-kunpeng920.sh` now downloads the pinned ARM64 one-click package, installs `nvme-cli` and `s3lvol_tgt`, creates a stable `cube-minio-s3lvol` service and bucket/config, creates the correctly sized WAL, and enables the systemd service under `multi-user.target`.

Verified live: service active and enabled, socket present, and 32 NVMe/TCP subsystems connected. Rerun idempotently:

```bash
scp scripts/recover-cubesandbox-s3lvol-kunpeng920.sh weitianc@193.124.7.2:/tmp/
ssh weitianc@193.124.7.2 'bash /tmp/recover-cubesandbox-s3lvol-kunpeng920.sh'
```

Do not delete `/data/cubelet/rcow/wal_bdev.img`, `bstore.json`, or the MinIO `cube-s3lvol` bucket now that the lvstore is formatted.

## Immediate live blocker

The paired smoke is not green. Current pod `cube-node-q2khk` is `2/3
CreateContainerError`. The problem is below CubeSandbox application code:
Docker metadata/start operations stall and `stat /sys/fs/cgroup` itself blocks
in an uninterruptible kernel/filesystem wait. Cube control-plane pods remain up.
Cubelet previously reached cubecow initialization but did not listen on 9999:

```text
starting cubelet ...
state dir /data/cubelet/state already mounted as xfs
set /data/cubelet/root as shared mount
... cubelet ... Killed
[cube-component:cubelet:run] ERROR: cubelet did not become ready
```

This is not host OOM: about 1.9 TiB is available and no kernel OOM record was found. `deploy/cubesandbox/cube-node-startup-timeout-patch.yaml` was applied to extend the entrypoint loop from 60 to 300 seconds, without resolving readiness. Diagnose the Cubelet/cubecow hang before the pair smoke:

```bash
ssh weitianc@193.124.7.2
systemctl status cube-sandbox-s3lvol.service
kubectl -n cube-system get pods -o wide
kubectl -n cube-system logs -l app.kubernetes.io/component=cube-node -c cubelet --tail=200
tail -200 /data/log/Cubelet/* 2>/dev/null
tail -200 /data/log/rcow/s3lvol_tgt.log
ps -eo pid,etime,rss,stat,wchan:30,cmd | grep '[c]ubelet'
```

Priority next: inspect the blocked `stat`, mount, containerd, and cubelet tasks
from a privileged node-debug/installer container, then recreate only the broken
DaemonSet pod if the host cgroup mount recovers. Do not delete persistent Cube
state merely to make it start, and do not reboot without user approval.

## Remaining work

1. Recover the Kunpeng cgroup/container runtime and return `cube-node` to 3/3.
2. Build/push/register the final Runtime, Tool, and Worker ARM64 images.
3. Verify the Runtime-to-Worker `/32` allow rule, exact execution-ID join,
   Tool cgroup/eBPF artifacts, and Runtime native ClawTune predictions in a real
   model-driven paired OpenClaw run.
4. Run the Kubernetes vertical slice, cancellation, controller restart, policy
   pause/resume, and zero-leak audit; save evidence and pin final digests.
5. Audit/remove inactive Kata/direct-Firecracker/SSH code after import checks.
   CubeSandbox remains the only active real sandbox.

## Continuation commands

```bash
# Repair Cubelet first.
ssh weitianc@193.124.7.2 'systemctl is-active cube-sandbox-s3lvol.service; kubectl -n cube-system get pods'

# Copy current code only to the clean remote copy.
rsync -az --delete --exclude .git --exclude .venv ./ weitianc@193.124.7.2:/home/weitianc/ClawBox-cube/

# Direct pair smoke.
ssh weitianc@193.124.7.2 'cd /home/weitianc/ClawBox-cube && CUBE_API_URL=http://127.0.0.1:30030 CUBE_PROXY_NODE_IP=127.0.0.1 CUBE_PROXY_PORT_HTTP=30080 /home/weitianc/.local/bin/uv run --with cubesandbox==0.7.0 --with e2b==2.29.5 python scripts/smoke-cubesandbox-agent-pair.py'

# Full local gate.
python -m compileall -q clawbox
python -m pytest -q --basetemp .pytest-tmp-handoff

# Leak audit after every live test.
ssh weitianc@193.124.7.2 'cd /home/weitianc/ClawBox-cube && /home/weitianc/.local/bin/uv run --with cubesandbox==0.7.0 --with e2b==2.29.5 python scripts/audit-cube-sandboxes.py'
```

Acceptance requires a real model-driven OpenClaw session in Runtime invoking authenticated `cube_shell` in the matching Tool VM, joined detailed traces/resources/timings, ClawBox lifecycle policy, and zero leaked sandboxes.
