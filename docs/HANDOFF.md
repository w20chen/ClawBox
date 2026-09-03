# ClawBox + ClawTune + CubeSandbox handoff (2026-09-03 20:45 CST)

This is the complete continuation point. Read it before changing the working tree or Kunpeng deployment.

## Final architecture decision

The newest user instruction overrides the older pasted specification that said one VM per agent. Each logical Agent uses exactly two CubeSandbox MicroVMs:

1. **Runtime VM**: OpenClaw, native ClawTune plugin/tracer, model access, reasoning state, and only an authenticated `cube_shell` tool.
2. **Tool VM**: `/workspace`, repository and command execution, cgroup/eBPF telemetry, and no model credential.

The ExperimentWorker owns both VM lifecycles, runs the authenticated bridge, applies ClawBox policy to the Tool VM, joins observations by execution/session identity, and writes full/redacted traces, action and lifecycle timings, resources, cgroup/eBPF data, policy events, provenance, and cleanup evidence.

## Repository and safety

- Local tree: `C:\Users\29068\Desktop\ClawBox`
- Last committed baseline: `3dca7df Complete CubeSandbox replay execution path`
- All paired-VM work is uncommitted; preserve it.
- Clean remote copy: `/home/weitianc/ClawBox-cube`
- Do not overwrite `/home/weitianc/ClawBox`; it is an older dirty checkout.
- ARM64 host: `ssh kunpeng`
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

Templates use `http://193.124.7.2:5001/...`. The read-only registry mirror is `clawbox-registry-cube`; after a reboot run `ssh kunpeng 'docker start clawbox-registry-cube || true'`.

## Implemented but not deployed end to end

- Specs/arms require separate `runtime` and `sandbox` (Tool) definitions.
- `worker.py` creates/cleans two owned lifecycles, reserves combined memory, applies policy to Tool, records role/timings, and runs OpenClaw through Runtime.
- `CubeSandboxLifecycle` supports creation environment variables.
- `openclaw_driver.py` runs OpenClaw inside Runtime, enables `cube-tool` and native ClawTune, denies host tools, injects model credentials only into Runtime, runs the authenticated Worker bridge, and copies Runtime JSONL traces.
- `openclaw-plugins/cube-tool/` implements `cube_shell`.
- `clawtune_trace.py` writes v6 tool spans and bridge records.
- Runtime/Tool Dockerfiles and Runtime entrypoint exist.
- CRD immutability includes credential secret and timeout; Worker Pod receives `CLAWBOX_BRIDGE_HOST`.
- Examples contain separate Runtime and Tool templates; registration supports repeated exposed/probe ports.
- Runtime-to-Worker requests now carry the OpenClaw tool-call ID into the
  authoritative bridge record, and Runtime creation adds a narrow CubeSandbox
  `network.allow_out` entry for only the Worker Pod IP.

Key files: `clawbox/experiments/worker.py`, `openclaw_driver.py`, `clawtune_trace.py`, `openclaw-plugins/cube-tool/index.js`, both Cube Dockerfiles, `scripts/smoke-cubesandbox-agent-pair.py`, and `examples/experiments/openclaw-cube.yaml`.

## Current verification

After the two-VM refactor this focused gate passes:

```text
python -m compileall -q clawbox
python -m pytest -o addopts= -q tests/test_experiments_v2.py tests/test_cube.py tests/test_controller_v2.py tests/test_openclaw_driver.py tests/test_clawtune_cube_trace.py
# 15 passed, 1 non-functional pytest cache warning
```

The full suite has not been rerun after the refactor.

## Storage recovery completed

After reboot, `no more resource` actually meant `/var/run/s3lvol.sock` was missing. The Helm chart does not install the host daemon. `scripts/recover-cubesandbox-s3lvol-kunpeng920.sh` now downloads the pinned ARM64 one-click package, installs `nvme-cli` and `s3lvol_tgt`, creates a stable `cube-minio-s3lvol` service and bucket/config, creates the correctly sized WAL, and enables the systemd service under `multi-user.target`.

Verified live: service active and enabled, socket present, and 32 NVMe/TCP subsystems connected. Rerun idempotently:

```bash
scp scripts/recover-cubesandbox-s3lvol-kunpeng920.sh kunpeng:/tmp/
ssh kunpeng 'bash /tmp/recover-cubesandbox-s3lvol-kunpeng920.sh'
```

Do not delete `/data/cubelet/rcow/wal_bdev.img`, `bstore.json`, or the MinIO `cube-s3lvol` bucket now that the lvstore is formatted.

## Immediate live blocker

The paired smoke is not green. Cubelet now reaches cubecow initialization but does not listen on 9999 before its wrapper kills it. Current pod was `cube-node-q2khk`, `2/3`, ending with:

```text
starting cubelet ...
state dir /data/cubelet/state already mounted as xfs
set /data/cubelet/root as shared mount
... cubelet ... Killed
[cube-component:cubelet:run] ERROR: cubelet did not become ready
```

This is not host OOM: about 1.9 TiB is available and no kernel OOM record was found. `deploy/cubesandbox/cube-node-startup-timeout-patch.yaml` was applied to extend the entrypoint loop from 60 to 300 seconds, without resolving readiness. Diagnose the Cubelet/cubecow hang before the pair smoke:

```bash
ssh kunpeng
systemctl status cube-sandbox-s3lvol.service
kubectl -n cube-system get pods -o wide
kubectl -n cube-system logs -l app.kubernetes.io/component=cube-node -c cubelet --tail=200
tail -200 /data/log/Cubelet/* 2>/dev/null
tail -200 /data/log/rcow/s3lvol_tgt.log
ps -eo pid,etime,rss,stat,wchan:30,cmd | grep '[c]ubelet'
```

Likely next: attach `strace -ff -p PID` from a privileged node-debug pod while Cubelet stalls and inspect `/data/log/Cubelet`. Determine whether it waits on NVMe, s3lvol RPC, gateway discovery, or stale state. Do not delete persistent state merely to make it start.

## Remaining work

1. Verify the new Runtime-to-Worker `/32` allow rule and exact tool-call ID
   against the real Runtime image and native ClawTune span output.
2. Tool telemetry: wrap every Tool command with the guest collector, cgroup v2 snapshots, and eBPF. Record explicit unavailable reasons; never fabricate values.
3. Build/push/pin the final ARM64 Worker, apply CRD/controller changes, and run a real OpenClaw job using a Kubernetes Secret.
4. Run focused/full tests, direct pair smoke, OpenClaw+ClawTune pair, Kubernetes vertical slice, cancellation, controller restart, policy pause/resume, and zero-leak audit. Save evidence.
5. Audit/remove inactive Kata/direct-Firecracker/SSH paths after import checks. CubeSandbox remains the only active real sandbox.

## Continuation commands

```bash
# Repair Cubelet first.
ssh kunpeng 'systemctl is-active cube-sandbox-s3lvol.service; kubectl -n cube-system get pods'

# Copy current code only to the clean remote copy.
rsync -az --delete --exclude .git --exclude .venv ./ kunpeng:/home/weitianc/ClawBox-cube/

# Direct pair smoke.
ssh kunpeng 'cd /home/weitianc/ClawBox-cube && CUBE_API_URL=http://127.0.0.1:30030 CUBE_PROXY_NODE_IP=127.0.0.1 CUBE_PROXY_PORT_HTTP=30080 /home/weitianc/.local/bin/uv run --with cubesandbox==0.7.0 --with e2b==2.29.5 python scripts/smoke-cubesandbox-agent-pair.py'

# Full local gate.
python -m compileall -q clawbox
python -m pytest -q --basetemp .pytest-tmp-handoff

# Leak audit after every live test.
ssh kunpeng 'cd /home/weitianc/ClawBox-cube && /home/weitianc/.local/bin/uv run --with cubesandbox==0.7.0 --with e2b==2.29.5 python scripts/audit-cube-sandboxes.py'
```

Acceptance requires a real model-driven OpenClaw session in Runtime invoking authenticated `cube_shell` in the matching Tool VM, joined detailed traces/resources/timings, ClawBox lifecycle policy, and zero leaked sandboxes.
