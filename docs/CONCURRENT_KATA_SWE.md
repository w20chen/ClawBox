# Concurrent SWE-ReBench Cells

> **Current-shape notice (2026-08-18):** this page was written for the earlier
> two-container design (init-container bridge install + native restartable
> sidecar). That design is **obsolete**. On the validated Kata host, containers
> cannot share volumes, so the Tool Pod is a **single container** (the bridge is
> baked into the task image) and the Runtime Job is a **single container** whose
> entrypoint starts ClawTune **in-process**. The authoritative description is
> [`AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md`](AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md)
> and the current renderers in `clawbox/cell/manifests.py`; this page is kept as
> design background and is updated below where it would otherwise mislead.

## Cell isolation model

Each `SandboxTask` owns one Tool Pod, one Runtime Job/Pod, one Service, one prompt ConfigMap, one task-scoped auth Secret, and four NetworkPolicies. Kubernetes owner references and the controller finalizer make reconciliation idempotent and cleanup explicit.

The Tool Pod is a **single container**: the ARM64 Tool Bridge is baked into the immutable task image (started via `/usr/local/bin/tool-bridge`) and runs as guest root inside the microVM on port 2222 with a task-specific Ed25519 key. The Tool side only ever holds the authorized public key and SSH host keys — never the runtime's private key, upload token, or any LLM material. The controller does not create the Runtime Job until the Tool container readiness probe succeeds.

The Runtime Job is also a **single container**. The `runtime-entrypoint`:

- starts the ClawTune sidecar **in-process** (`/usr/local/bin/clawtune-sidecar-entrypoint` as a background process) in `observe` / `hook-only` mode, fail-open, with cgroup/affinity/NUMA disabled;
- runs OpenClaw, which reaches only the Tool Bridge, the LLM CIDR, DNS, and the ingester;
- writes `result.json` and signals `.runtime-complete`; the sidecar stops, flushes trace chunks, uploads result and final marker, verifies the central receipt, and only then writes `.upload-complete`, after which the entrypoint exits with the agent status. A successful Job therefore implies durable result plus final trace receipt.

Guest root inside the microVMs is a documented, probe-verified compatibility form (Kata's agent writes Secret/ConfigMap volume data dirs with mode `0000`); the microVM is the isolation boundary. Supervisor/non-root separation for task commands is scheduled for M2 (CBX-M2-001) and is a release blocker before external tenants are allowed.

## Admission

The controller runs one replica and serializes reconciliation. It reserves the complete Cell before creating either VM:

```text
reservation = (runtime incl. in-process ClawTune) + tool + 2 × RuntimeClass overhead + 10% safety
```

It evaluates CPU, memory, ephemeral/devmapper storage, and two Pod slots together. Capacity is ARM64 ready-node allocatable minus non-Cell Pod requests, bounded by the captured devmapper budget. Existing nonterminal `SandboxTask` reservations are reconstructed from CR status after a controller restart.

Profiles are deliberately conservative; the ClawTune budget is merged into the Runtime container because ClawTune runs in-process in the same container:

| Profile | Runtime container (incl. in-process ClawTune) | Tool | Cell behavior |
|---|---:|---:|---|
| small | 1.25 CPU / 2.5 GiB | 2 CPU / 4 GiB | default validation |
| medium | 2.25 CPU / 4.5 GiB | 4 CPU / 8 GiB | normal repository work |
| large | 4.25 CPU / 8.5 GiB | 8 CPU / 16 GiB | heavy builds/tests |

Each row also includes the two 250m/256Mi RuntimeClass overheads and storage budgets defined in code. `CellSizer`, `NodeCapacityProvider`, and `PlacementPolicy` are interfaces; ClawTune prediction has a fail-closed extension point and is not active in observe-only mode.

## State machine and failure semantics

```text
Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning → Collecting
                                                            ├→ Succeeded
                                                            ├→ Failed
                                                            └→ TimedOut
                                                                  ↓
                                                               Cleaned
```

- invalid or mutable images fail before admission;
- insufficient whole-Cell capacity leaves the task queued;
- Tool failure prevents Runtime creation;
- Runtime success without a durable upload receipt is impossible by entrypoint contract;
- timeouts and container failures become terminal before cleanup;
- deletion always invokes the finalizer cleanup path.

## Scale procedure

Run the stage-0 smoke first, ensure no active Cells when capturing the devmapper capacity baseline, then deploy the ConfigMap:

```bash
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
bash scripts/scale-swe-rebench.sh \
  --tasks /data/tasks.json --arm64-map /data/arm64-map.json \
  --llm-egress-cidr 203.0.113.10/32
```

The script runs 1, 2, 4, 8, 16, and 32 concurrent tasks in order, stopping on the first task or storage gate failure. Record scheduling latency, microVM boot latency, task latency, CPU/memory pressure, thin-pool utilization, network errors, and artifact completeness for each step. A 320-core host is a capacity target, not permission to bypass these measured gates.
