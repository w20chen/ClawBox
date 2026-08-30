# Execution architecture and mode inventory

ClawBox runs are described as independent axes, not one overloaded "mode". A
workflow is one validated selection of workload source, agent driver, inference
backend, sandbox backend, Tool transport, admission policy, residency policy,
and result collection. A baseline is only an immutable scheduling-policy bundle.

## Canonical axes

| Axis | Values currently named by the schema |
|---|---|
| Workload source | `swe_rebench`, `recorded_trace`, `synthetic` |
| Agent driver | `openclaw`, `replay_engine` |
| Inference backend | `api`, `replay` |
| Sandbox backend | `kubernetes`, `direct_firecracker`, `local` |
| Tool transport | `ssh`, `vsock`, `local`, `kubectl` |
| Admission policy | `fixed_profile`, `fixed_explicit`, `p90_static`, `p90_elastic` |
| Residency policy | `resident`, `llm_wait_checkpoint`, `pressure_checkpoint` |

`vm_checkpoint` means Firecracker execution state. `kb_snapshot` means
ClawTune knowledge state, `egress_address_snapshot` means resolved provider
addresses, and `storage_snapshot` means containerd/devmapper state. New APIs
must use those terms rather than an unqualified `snapshot`.

## Current public runner inventory

| Public entry point | Workload / driver | Backend and topology | Policy / result | Status |
|---|---|---|---|---|
| `scripts/run-swe-rebench.sh` → `clawbox.benchmark.kubernetes` | SWE-ReBench / OpenClaw | Kubernetes Kata/Firecracker ARM64, Runtime Job + Tool Pod, SSH | `fixed_profile`, `resident`; SandboxTask outcome JSON plus result envelope | production |
| `clawbox.benchmark.managed_client` and `clawbox.benchmark.multitenant` | Managed SWE-ReBench intake / dispatcher | Managed API persistence then accepted Cell path | managed run/attempt/event records | production intake |
| `clawbox.cell.app` | SandboxTask controller | Kubernetes Runtime + Tool Cell | fixed default; opt-in bounded `p90_static`/`p90_elastic` Tool sizing | production default plus research baselines |
| `python -m clawbox.replay.cli study|suite` / `clawbox-replay` | one or several recorded traces / OpenClaw | two direct Firecracker VMs per task, Runtime + Tool, SSH | fixed/P90 sizing crossed with `resident`/`llm_wait_checkpoint`; study JSON and envelopes | research |
| `scripts/run-openclaw-experiment.py` | prepared recorded task / OpenClaw | two direct Firecracker VMs, SSH | `--mode snapshot` remains an alias for `llm_wait_checkpoint` | research adapter |
| `python -m clawbox.replay.cli run|experiment` / `clawbox-replay` | recorded trace / ReplayEngine | direct Firecracker or local; transport is selected explicitly, and paired Tool Firecracker requires `vsock` | its own resident slots, lifecycle and latency-triggered checkpoint policy; JSONL | historical mechanism testing |
| `clawbox.scheduler`, `clawbox.allocator`, `clawbox.controller` | execution intent / legacy controller | Tool-only subprocess, Docker, or Kubernetes paths | p90 allocation may be used here; not a two-VM Cell | legacy |
| guest/Firecracker/transport smoke helpers | synthetic smoke input | direct Firecracker or local | targeted lifecycle/transport assertions | local-only |

The remaining console entry points are supporting services or build/operator
tools, not additional execution workflows: Cell/managed/trace/tuning services
serve the production path; node/tool agents serve legacy control-plane paths;
the ARM64 image commands materialize images; and `clawbox` is the operator
client. They therefore do not introduce hidden workload, inference, admission,
or residency combinations.

Production run, attempt, event, outbox, and audit persistence is authoritative
in `clawbox.managed`. The similarly named tables in `clawbox.common.db` are
legacy/dev-stage storage and are not another production workflow.

The default production workflow is: `swe_rebench` + OpenClaw + API +
Kubernetes + SSH + `fixed_profile` + `resident`. The opt-in research Cell
baselines use `p90_static` or `p90_elastic` admission with resident VMs. Static
requires an exact generation. Elastic resolves latest once at admission; both
persist the exact prediction and full Cell size so reconciliation cannot drift.
Neither performs Kubernetes checkpointing.

The active paper study can cross `inference_backend = replay | api`,
`sizing_policy = fixed | p90_static`, and `residency_policy = resident |
llm_wait_checkpoint`. The suite additionally crosses workloads and concurrency
levels under a validated NUMA CPU/memory budget. Its legacy config spelling
`memory_policies: [resident, snapshot]` remains supported and translates at the
boundary.

## Canonical planning interface

`clawbox.experiments` is read-only planning and metadata code. It does not own
runner lifecycles or materialize sandboxes.

```bash
python -m clawbox.experiments.cli list-baselines
python -m clawbox.experiments.cli validate experiment.json
python -m clawbox.experiments.cli resolve experiment.json
python -m clawbox.experiments.cli matrix experiment.json
```

The immutable registry currently exposes:

| Baseline | Admission | Residency | Implementation status |
|---|---|---|---|
| `fixed-resident` | `fixed_profile` | `resident` | accepted production Cell |
| `fixed-explicit-resident` | `fixed_explicit` | `resident` | direct-Firecracker research |
| `fixed-llm-wait-checkpoint` | `fixed_explicit` | `llm_wait_checkpoint` | direct-Firecracker research |
| `p90-static` | `p90_static` | `resident` | Kubernetes and direct-Firecracker research |
| `p90-static-llm-wait-checkpoint` | `p90_static` | `llm_wait_checkpoint` | direct-Firecracker research |
| `p90-elastic` | `p90_elastic` | `resident` | Kubernetes research; frozen at admission |
| `p90-elastic-pressure-checkpoint` | `p90_elastic` | `pressure_checkpoint` | not implemented |

Unsupported combinations are rejected before a runner starts: Kubernetes
checkpoint residency, pressure checkpointing, and local Firecracker
checkpointing are not claimed. Tool-only legacy execution
is likewise never represented as a Runtime + Tool Cell.

All new outer results use `ResultEnvelope`: run/case IDs, complete resolved
workflow, classification, stable status, failure category, metrics, artifacts,
and backend details. Production envelopes record actual launcher concurrency
and the case-specific Tool image. Paper envelopes record selected inference
configuration, VM materialization, resources, and one success or failure per
session. Existing production result JSON, replay JSONL, and study summaries
remain detailed artifacts referenced by that envelope.
