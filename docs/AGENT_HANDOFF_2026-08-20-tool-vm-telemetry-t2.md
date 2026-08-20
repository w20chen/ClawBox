# Agent Handoff: Tool-VM Telemetry T2

Date: 2026-08-20

This handoff is self-contained. Read it with
`docs/NEXT_PLAN_TOOL_VM_TELEMETRY.md`; older G0 handoffs are historical. The
program goal is T0-T4. T0 and T1 are complete. T2 is implemented but has not
passed its production-image gate. T3 and T4 have not started.

## Repository state

ClawBox branch `main`:

- T0: `0bae91a`.
- T1: `9c26605` (last completed milestone).
- The current tree contains incomplete T2 plus this handoff. Commit it as
  incomplete work, not as a completed T2 milestone.

Sibling ClawTune branch `v2` (all pushed):

- `3cb0793`: honest resource source provenance (T0).
- `fcdec7c`: native explicit-cgroup guest smoke (T1).
- `65077f9`: authenticated guest-local collector service.
- `fdc9013`, `7ff4d6d`: BCC tracefs normalization.
- `a493ab7`: mount a legacy tracefs view.
- `984861a`: overlay a masked guest debugfs (latest).

ClawTune was clean after `984861a`. Its remote warns that the repository moved
to `git@github.com:w20chen/ClawTune.git`, although the configured URL still
accepted pushes.

## What incomplete T2 implements

- `toolbridge/guest_collector.go`: authenticated versioned Unix-socket client
  and collector supervisor. It uses a random 32-byte token and supports health,
  begin, finish, abort, and shutdown.
- `toolbridge/main.go`: observe-before-exec gating, finalization before cgroup
  cleanup, and explicit fail-open telemetry state. Exit codes, timeout 124,
  output framing, and SSH behavior are preserved.
- The gate starts `/bin/sh -lc` before attribution so login-profile helpers are
  excluded, then releases `exec /bin/sh -c "$CLAWBOX_GATE_COMMAND"`. Including
  profile helpers made ClawTune artifacts ineligible.
- `toolbridge/collector.go`: exclusive non-root per-execution cgroup-v2 leaves.
  It does not write subtree controllers, which Kata does not delegate here.
- ClawTune `tools/guest_collector_server.py`: authoritative native collection
  inside the Tool container and therefore against the Firecracker guest kernel,
  never the physical host kernel.
- `docker/Dockerfile.swe-rebench-tool-telemetry`: Ubuntu 22.04 production
  overlay with distro BCC, exact guest-kernel headers, pinned ClawTune source,
  helper, validator, and bridge.
- ClawTune is imported from pinned source using `PYTHONPATH`. Do not restore the
  removed `pip install`: it needlessly resolved the complete sidecar dependency
  graph and hung on external package access. Standalone ClawTune packaging was
  not changed.
- `scripts/rebuild-swe-rebench-tool-overlay.sh`: accepts and validates an exact
  `CLAWTUNE_REVISION`, embeds it as an OCI label, verifies ARM64, pushes, and
  prints the immutable digest.
- Tool Pods use `kata-fc-arm64-ebpf`; Runtime Pods remain on
  `kata-fc-arm64`. Tool capabilities are `SYS_ADMIN`, `NET_ADMIN`, `NET_RAW`,
  and `SYS_PTRACE`.
- `scripts/runtime-entrypoint.sh` retrieves both `cgroup-resource-*.json` and
  `clause-telemetry-*.json`.
- `scripts/run-toolbridge-ebpf-integration.sh` runs a real SSH bridge workload:
  CPU-heavy command, pipeline, exit 7, timeout 124, two concurrent commands,
  and forced helper death/fail-open.
- `scripts/validate-toolbridge-guest-artifacts.py` requires seven execution
  records, controlled native artifacts, distinct concurrent cgroups, zero loss,
  cleanup success, nonzero CPU/RSS, no raw command leakage, and explicit
  fail-open state.

## Verified evidence

- RuntimeClass: `kata-fc-arm64-ebpf`.
- Guest kernel: `6.18.28`.
- Kernel source SHA-256:
  `f360789483586cf8a20b4ab2bffe76ead6b62c0db1eeb0d917294456c4d77b74`.
- Immutable SWE-Rebench base:
  `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:d424fb2440bbf8d055f8846d6ba783fb558a8185d5c6436c3260e627209f611a`.
- T1 native smoke passed in real ARM64 Firecracker: native BPF compile/attach,
  nonzero CPU/RSS, zero loss, valid integrity, cleanup, and an eligible call.
- The T2 research image passed the full bridge acceptance: seven executions,
  isolated concurrent cgroups, about one CPU core and 2.34 MB peak RSS for the
  long command, six valid native artifacts, zero loss, cleanup, and explicit
  helper fail-open.
- Focused ClawTune guest/telemetry suite after `984861a`: 43 passed on ARM64.
- Earlier broad ClawTune run: 607 passed and one pre-existing ARM-sensitive
  strict-eBPF test failed outside this guest service.
- ClawBox Kubernetes backend tests: 20 passed.
- Native ARM64 `go test -race ./...`: passed.

## Exact unresolved production failure

The last completed production image is:

`127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:c61a31898ce742919fb582a32942afc74e3656bb92aa2f07313195c4306e4e2a`

It embeds ClawTune `a493ab7e6e97ad017a472f38eedc08cd7842e294`.
The workload passed, including exit 7 and timeout 124, but the helper failed:

```text
PermissionError: [Errno 1] Operation not permitted:
'/sys/kernel/debug/tracing'
```

Root-cause history:

1. Canonical `/sys/kernel/tracing` mounts and contains the tracepoint.
2. Ubuntu 22.04 BCC 0.18 still opens the legacy
   `/sys/kernel/debug/tracing/events/sched/sched_process_exit/id` from libbcc,
   even when Python `bcc.TRACEFS` is corrected.
3. `a493ab7` treated `Path('/sys/kernel/debug').is_mount()` as readiness. Kata
   exposes an unusable read-only mask there, so the helper skipped overlaying
   debugfs and could not create `tracing`.
4. ClawTune `984861a` now uses presence of the required tracepoint as readiness
   and overlays debugfs otherwise. Unit tests pass. Its production build was
   interrupted for this handoff before producing a digest, so it is not proven.

Do not weaken the validator and do not call fail-open execution a telemetry
success. T2 requires valid native artifacts from the production image.

## Shortest takeover sequence

Remote: `weitianc@193.124.7.2` (SSH key, partial sudo). It has a stale SOCKS
proxy; use `git -c http.proxy= -c https.proxy=`. Do not delete unrelated pods.

Remote isolated paths:

- ClawBox build tree: `/tmp/clawbox-t0-20260820`.
- Exact ClawTune `984861a` archive tree: `/tmp/clawtune-t2-984861a`.
- Registry: `127.0.0.1:5000/clawbox`.

The interrupted `t2-handoff-final` Docker processes were explicitly stopped.
Rebuild the exact source:

```bash
CLAWBOX_ROOT=/tmp/clawbox-t0-20260820 \
BASE_IMAGE=127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:d424fb2440bbf8d055f8846d6ba783fb558a8185d5c6436c3260e627209f611a \
CLAWTUNE_ROOT=/tmp/clawtune-t2-984861a \
CLAWTUNE_REVISION=984861a6bec50e397cc3f31cfec9bb2fd8c79880 \
TAG=t2-984861a \
/tmp/clawbox-t0-20260820/scripts/rebuild-swe-rebench-tool-overlay.sh
```

Run the printed immutable digest, not the tag:

```bash
cd /tmp/clawbox-t0-20260820
CLAWBOX_EBPF_IMAGE='<printed image@sha256:digest>' \
CLAWBOX_EBPF_NAMESPACE=clawbox-t2-production-984861a \
./scripts/run-toolbridge-ebpf-integration.sh
```

If it still fails, retain a purpose-built debug Pod and inspect mountinfo and
the mount return code. Likely alternatives are mounting tracefs directly over
an existing legacy mountpoint, adding an OCI/Kata mount exposing canonical
tracefs at the legacy path, or using a newer BCC package/backend in ClawTune.
The solution must stay inside the Tool VM guest and preserve standalone
ClawTune behavior.

After acceptance, rerun focused ClawTune tests, the ClawBox Python suite, and
ARM64 Go race tests; update the plan; then commit/push completed T2.

## T3 and T4 are untouched

T3 must add immutable signed native manifests, tenant/repository identity,
idempotent raw storage, pinned ClawTune validation, and atomic native
`ClauseResourceKB` plus `RuntimeToolResourceKB` snapshots. Do not make
`clawbox/tuning/clawtune.py` the source of truth.

T4 must prove a two-run shadow loop: run A advances generation N to N+1; run B
loads exactly N+1 and records a native prediction whose evidence includes run
A. `FixedProfileSizer` remains authoritative until safety metrics pass.

Only after T4 should managed sandbox lifecycle convergence resume.

## Incomplete T2 file inventory

- `clawbox/cell/app.py`, `clawbox/cell/manifests.py`, `clawbox/common/config.py`
- `deploy/runtimeclass-firecracker-ebpf.yaml`
- `docker/Dockerfile.tool-bridge`, `docker/Dockerfile.tool-telemetry-research`
- `docker/Dockerfile.swe-rebench-tool-telemetry`
- `scripts/build-tool-telemetry-research.sh`
- `scripts/rebuild-swe-rebench-tool-overlay.sh`
- `scripts/run-toolbridge-ebpf-integration.sh`
- `scripts/runtime-entrypoint.sh`
- `scripts/validate-toolbridge-guest-artifacts.py`
- `tests/test_kubernetes_backend.py`
- `toolbridge/collector.go`, `toolbridge/guest_collector.go`
- `toolbridge/guest_collector_test.go`, `toolbridge/go.mod`, `toolbridge/go.sum`
- `toolbridge/main.go`

Generated `.artifacts/` archives are ignored and must not be committed.
