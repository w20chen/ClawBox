# Concurrent SWE-ReBench Cells

## Cell isolation model

Each `SandboxTask` owns one Tool Pod, one Runtime Job/Pod, one Service, one prompt ConfigMap, one task-scoped auth Secret, and four NetworkPolicies. Kubernetes owner references and the controller finalizer make reconciliation idempotent and cleanup explicit.

The Tool Pod starts first. An init container copies the static ARM64 Tool Bridge into an `emptyDir`; the immutable task image then runs that binary as UID 10001 on port 2222 with a task-specific Ed25519 key. The controller does not create the Runtime Job until the Tool container readiness probe succeeds.

The Runtime Pod has two containers:

- the main OpenClaw runtime, which reaches only the Tool Bridge, the LLM CIDR, DNS, and the ingester;
- a restartable init container (`restartPolicy: Always`) running the native ClawTune sidecar in `observe-only` / `hook-only` mode.

The containers share only a Pod-local `emptyDir`. At completion, runtime writes the result and signals ClawTune; the sidecar stops, flushes trace chunks, uploads result and final marker, verifies the central receipt, and only then acknowledges runtime exit. A successful Job therefore implies durable result plus final trace receipt.

## Admission

The controller runs one replica and serializes reconciliation. It reserves the complete Cell before creating either VM:

```text
reservation = runtime + tool + sidecar + 2 × RuntimeClass overhead + 10% safety
```

It evaluates CPU, memory, ephemeral/devmapper storage, and two Pod slots together. Capacity is ARM64 ready-node allocatable minus non-Cell Pod requests, bounded by the captured devmapper budget. Existing nonterminal `SandboxTask` reservations are reconstructed from CR status after a controller restart.

Profiles are deliberately conservative:

| Profile | Runtime | Tool | Sidecar | Cell behavior |
|---|---:|---:|---:|---|
| small | 1 CPU / 2 GiB | 2 CPU / 4 GiB | 0.25 CPU / 512 MiB | default validation |
| medium | 2 CPU / 4 GiB | 4 CPU / 8 GiB | 0.25 CPU / 512 MiB | normal repository work |
| large | 4 CPU / 8 GiB | 8 CPU / 16 GiB | 0.25 CPU / 512 MiB | heavy builds/tests |

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
