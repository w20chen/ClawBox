# Baseline definitions

This document defines what each ClawBox baseline does. A baseline is a complete
policy tuple, not only an admission estimator. The same workload, templates,
workspace, trace timing, memory budget, NUMA scope, and seed must be used when
comparing baselines.

## Common admission model

Before native SSH starts, PolicyControl checks the arm's current host physical
memory sample, existing reservations, the requested incremental reservation,
and `checkpoint_restore_headroom_mib` against `pool_memory_budget_mib`. The
common `emergency_free_memory_mib` guard remains enabled for every baseline.
Admission is FIFO and synchronous. A reservation is released only after the SSH
process exits and its telemetry/completion records have been collected.

The configured VM memory is not itself the same as measured host memory. The
Runtime/Tool VM pair is the offered footprint; host `MemAvailable` and VM RSS
measure actual physical residency. The baseline controls the reservation and
residency policy, not the Tool command semantics.

## Admission baselines

### `lifetime_full`

Policy tuple:

```yaml
admission: lifetime_full
reclamation: resident
eviction: none
restore: none
```

At session creation, reserve the full Runtime + Tool configured memory for the
entire Agent lifetime. Tool calls do not add a second incremental Tool
reservation. Both VMs stay resident.

This is the conservative capacity baseline. It answers: “How does the system
behave when every offered Agent receives a complete lifetime memory claim?” It
usually blocks earliest under a constrained pool, but should not be described
as a measured Tool-memory predictor.

### `tool_full`

Policy tuple:

```yaml
admission: tool_full
reclamation: resident
eviction: none
restore: none
```

Keep both VMs resident, but reserve `full_tool_memory_mib` for each active Tool
operation. If that field is absent, the configured Tool VM memory is used. This
isolates the effect of lifetime Runtime reservation from a full Tool-operation
reservation.

Use it as the conservative resident Tool baseline. It is not command-specific
and does not claim that each command actually uses the full Tool allocation.

### `tool_static`

Policy tuple:

```yaml
admission: tool_static
reclamation: resident
eviction: none
restore: none
```

Keep both VMs resident and reserve the fixed `static_tool_memory_mib` amount
for each Tool operation. This is a static resident admission baseline. The
value must be recorded in the spec and kept identical across arms being
compared.

If the static amount is too small, the common safety guard may intervene or a
Tool may fail under pressure; those events must remain visible. Do not call a
static estimate “P90”.

### `tool_p90`

Policy tuple for the resident form:

```yaml
admission: tool_p90
reclamation: resident
eviction: none
restore: none
```

Before each Tool SSH invocation, resolve the canonical command key in the
immutable `resources.p90_predictions` artifact. The reservation is the
calibrated host VM-RSS increment from that command's ClawTune P90 profile.
Runtime command metadata, canonical key, prediction source, fallback level,
and artifact SHA must agree before admission. Missing or mismatched prediction
metadata fails closed; it must not silently become a global default.

This baseline measures command-specific prediction without VM reclamation. A
valid result reports prediction count, source distribution, fallback rate, P90
error, underestimate, and reservation accuracy.

### `tool_oracle`

Policy tuple:

```yaml
admission: tool_oracle
reclamation: resident
eviction: none
restore: none
```

Use held-out actual measurements supplied by `resources.oracle_measurements`.
This is an evaluation upper bound, not an implementable online policy. It must
be labeled `oracle` everywhere and must never be mixed into the proposed-policy
claim or used as training data for the same held-out trajectory.

## Residency and reclamation baselines

These policies compare CubeSandbox residency. They can be combined with
`tool_static` or `tool_p90`; the admission estimator and reclamation mechanism
must be named separately in the report.

### `resident`

```yaml
reclamation: resident
eviction: none
restore: none
```

Runtime and Tool remain resident for the Agent lifetime. No model-wait pause or
restore occurs. This is the direct residency baseline for measuring the memory
benefit and service cost of snapshot reclamation.

### `snapshot_pause` + `eager` + `reactive`

```yaml
reclamation: snapshot_pause
eviction: eager
restore: reactive
```

When a model request begins and the policy chooses reclamation, the system
waits for Tool activity to become idle, pauses/checkpoints Tool and Runtime,
and observes host memory before and after. Runtime is restored before the
pending model response is released. Tool remains paused until the next Tool
admission, which restores Tool and re-resolves its semantic TCP endpoint.

This is the direct snapshot comparison. It is “eager” because the pause is
scheduled immediately at the eligible model wait, not after a fixed timer or
only after a later pressure event.

### `snapshot_pause` + `fixed_delay` + `reactive`

```yaml
reclamation: snapshot_pause
eviction: fixed_delay
fixed_delay_seconds: 0.5
restore: reactive
```

Wait the configured delay after the model request before pausing. This tests
whether an immediate snapshot is too aggressive for short waits. The delay is
causal policy behavior and must not be compressed in the primary replay.

### `snapshot_pause` + `wait_aware_pressure` + `reactive`

```yaml
reclamation: snapshot_pause
eviction: wait_aware_pressure
restore: reactive
```

Pause only when request-time wait information is available and the configured
memory-pressure predicate is true. The policy may leave a VM resident during a
short or non-pressured model wait. The wait estimate must be recorded with its
source; actual future wait duration cannot be used to make the decision.

### `snapshot_pause` + `wait_aware_pressure` + `proactive`

```yaml
reclamation: snapshot_pause
eviction: wait_aware_pressure
restore: proactive
prefetch_lead_seconds: 0.5
```

This is the proposed combined decision variant when paired with `tool_p90`.
It uses request-time wait prediction for reclamation and schedules Runtime
restore before the predicted response by `prefetch_lead_seconds`. Tool restore
remains admission-scoped; proactive Runtime restore does not mean eager Tool
restore.

It is valid only when the wait prediction and provenance are present. If the
prediction is missing, the arm must record that no proactive decision was made;
do not substitute the actual observed wait.

## Canonical comparison matrix

The minimum useful paper comparison is:

| Name in report | Admission | Residency | Research question |
| --- | --- | --- | --- |
| Lifetime-full resident | `lifetime_full` | `resident` | Conservative full Agent capacity. |
| Tool-full resident | `tool_full` | `resident` | Full Tool reservation without lifetime Tool claim. |
| Tool-static resident | `tool_static` | `resident` | Static Tool reservation without reclamation. |
| Tool-P90 resident | `tool_p90` | `resident` | Command-specific KB admission only. |
| Static snapshot | `tool_static` | eager paired snapshot | Reclamation benefit independent of P90. |
| Proposed combined | `tool_p90` | wait-aware/proactive paired snapshot | Prediction + context + reclamation. |
| Oracle | `tool_oracle` | explicitly labeled | Upper bound only. |

Add fixed-delay and reactive wait-aware variants when studying decision timing.
Do not silently replace a baseline with a compatibility alias. Compatibility
names in `clawbox/experiments/baselines.py` are retained for older specs and
must be described as aliases in a report.

## What to report for every baseline

For each arm, report status/correctness first, then:

- admission count, blocked wait distribution, safety interventions;
- Agent throughput and JCT mean/p50/p90/p95;
- Tool latency and model/tool steps;
- host mean/peak and memory-time;
- Tool guest measured memory and host execution increment;
- prediction source, fallback rate, P90 error, and KB SHA when applicable;
- pause/restore counts, service time, bytes reclaimed, and response hold;
- exact telemetry join/loss, wrong or duplicate Tool executions, OOM, and leaks.

The interpretation rules and result-file locations are in
[results-guide.md](results-guide.md).
