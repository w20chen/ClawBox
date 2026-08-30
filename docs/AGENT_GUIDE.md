# ClawBox Maintainer Guide

This is the durable handoff for future code agents. Read the code and Git
history before changing behavior; this file records invariants and the latest
real-machine acceptance, not a substitute for verification.

## System shape

One task creates a two-microVM Cell on an ARM64 Kubernetes node:

```text
SandboxTask
├── Tool Pod: task image + SSH Tool Bridge + command processes
└── Runtime Job: OpenClaw + in-process ClawTune sidecar
```

Both workloads use Kata Containers with Firecracker. The Tool VM uses the
separate `kata-fc-arm64-ebpf` RuntimeClass because command telemetry must run
against the Tool guest kernel. The Runtime VM must never be used as the source
of per-command kernel data, and the physical host can only observe a VM
process—not the command process tree inside it.

The live controller currently consumes `SandboxTask` `v1alpha1`. The managed
dispatcher can render the newer identity envelope, but deployments using the
provided live CRD must set `CLAWBOX_CR_VERSION=v1alpha1` until controller and
conversion-webhook convergence is completed.

## Accepted production baseline

The latest accepted source and images are:

- ClawTune: `76eab6fa5c6333f4e80901c030f10cab0e4ce605`.
- Production Tool image:
  `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:c930c7243e6072924a812b1102356e5c972f3d2e2ef1082a6d4cf0510eb997cd`.
- Control-plane image:
  `127.0.0.1:5000/clawbox/control-plane-arm64@sha256:72907f6203e03a9ba1c2616814b2fea540fa6c5d9efcb4df464ce190d707230e`.
- Runtime image:
  `127.0.0.1:5000/clawbox/runtime-arm64@sha256:6215a4b10feec3ba8d6dcd39e763b92322fe7605dba62c39bcdae8bc71d04e44`.

Acceptance ran on real ARM64 hardware with Ubuntu 22.04 task images,
Kata/Firecracker, guest kernel 6.18.28, and Ubuntu BCC 0.18. The strict run
proved BPF compilation, load, attachment, native clause artifacts, non-zero
CPU/RSS, zero loss, cleanup, distinct concurrent cgroups, no
cross-attribution, and explicit isolated fail-open behavior.
The Tool Bridge must mount tracefs before starting BCC; the Kata guest exposes
the tracepoints but does not mount tracefs by default.

The feedback acceptance used five valid Run A execution pairs to advance the
native KB from generation 0 to 1. Run B loaded exactly generation 1, produced
`repo:exact_clause` and `repo:exact_command` predictions whose evidence named
Run A, and joined the prediction to real cgroup-v2 actuals. Predictions remain
shadow-only and `FixedProfileSizer` remains authoritative.

## Non-negotiable invariants

1. Per-command eBPF stays inside the Tool Firecracker guest kernel.
2. A command succeeding while telemetry fails is fail-open execution, not a
   successful telemetry sample. Such a sample cannot train the KB.
3. Native ClawTune readers and validators are authoritative. Do not weaken or
   replace them with a ClawBox approximation.
4. Eligible artifacts require exact clause/cgroup execution identity pairing,
   valid cleanup, zero event loss, valid collection quality, and non-zero
   resource values where required.
5. Raw native manifests are signed, digest-addressed, immutable, and scoped by
   tenant and repository. Replays are idempotent; cross-tenant reuse fails.
6. `ClauseResourceKB` and `RuntimeToolResourceKB` publish as one atomic pair.
7. Runtime and Tool credentials remain separated. The Tool VM never receives
   LLM or central artifact credentials.
8. Production task images are native ARM64 immutable digests. There is no
   x86, runc, QEMU, or alternate-VMM fallback.
9. Never clean unrelated Kubernetes namespaces, Pods, Kata sandboxes, or
   devmapper snapshots during debugging.
10. The Kata shim wrapper must raise its inherited soft `RLIMIT_NOFILE` to the
    hard limit before starting runtime-rs. The live audit requires at least
    8192.

## Code map

- `clawbox/cell/`: Cell validation, capacity, reconciliation, and Kubernetes
  manifests.
- `clawbox/api/` and `clawbox/managed/`: tenant-scoped run API, idempotency,
  attempts, events, and dispatch outbox.
- `toolbridge/`: SSH execution protocol, per-execution cgroups, and guest
  collector lifecycle.
- `clawbox/tuning/`: signed ingestion, immutable native storage, projection,
  atomic snapshots, rollback, and offline analysis.
- `scripts/runtime-entrypoint.sh`: runtime orchestration, native KB load,
  sidecar startup, artifact retrieval, shadow report, flush, and upload.
- `docker/Dockerfile.swe-rebench-tool-telemetry`: accepted production Tool
  overlay with BCC, exact guest headers, Tool Bridge, and pinned ClawTune.
- `scripts/run-toolbridge-ebpf-integration.sh`: strict guest eBPF acceptance.
- `scripts/validate-toolbridge-guest-artifacts.py`: acceptance requirements;
  do not lower them to make a test pass.

## Current boundaries

The canonical execution axes, capability rules, runner classification, and
baseline registry are summarized in
[Execution architecture](execution-architecture.md). New production or paper
metadata must resolve through that model; legacy runners remain separate.

- The accepted deployment is single-node ARM64. Multi-node scheduling, leader
  election, and HA reservation consistency are not production-accepted.
- The managed API, dispatcher, terminal projection, and cancellation
  convergence are implemented for the supplied `v1alpha1` deployment. The
  `v1alpha2` conversion path remains outside the accepted live baseline.
- The supplied trace and tuning manifests use node-local persistence suitable
  for the accepted single-node environment; external deployments should use
  durable PostgreSQL/object storage.
- Network counters are not yet reliable at clause granularity.
- Native predictions are observational. Do not connect them to resource
  enforcement without explicit safety gates for coverage, calibration,
  underprediction, over-allocation, loss, fallback, and task success.

## Required verification

For ordinary changes:

```bash
python3 -m pytest -q
python3 -m py_compile $(find clawbox scripts -name '*.py')
```

For Tool Bridge changes on native ARM64:

```bash
cd toolbridge
go test -race ./...
```

Before a Firecracker acceptance run, install/verify the shim wrapper and cache
sudo credentials if the smoke is launched non-interactively:

```bash
sudo bash scripts/install-shim-nofile-wrapper.sh
sudo -v
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

For ClawTune integration changes, run the pinned native readers plus the
focused guest collector/telemetry suite. For any production Tool-image change,
rebuild to an immutable digest and run:

```bash
bash scripts/run-toolbridge-ebpf-integration.sh \
  --image REGISTRY/TOOL@sha256:DIGEST \
  --namespace clawbox-ebpf-acceptance \
  --output .artifacts/tool-ebpf-acceptance.log
```

Do not call the image accepted unless the script exits zero and its native
artifact validator passes all strict checks.

Historical failure objects may be removed only after a dry run confirms they
are in API phase `Failed`:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py --namespace clawbox-system --apply
```
