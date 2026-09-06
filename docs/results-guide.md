# Results guide

Each run writes one result directory. Treat the whole directory as the result;
`summary.md` is only a quick index.

## Find or copy a run

```bash
clawbox --output-root /data/clawbox-results experiment status <run-id>
clawbox --output-root /data/clawbox-results experiment collect <run-id>
```

To copy a Kunpeng run:

```bash
scp -r kunpeng:/home/weitianc/clawbox-results-current/<run-id> ./results/
```

For a large run, archive the complete directory first and store its SHA-256.

## Directory contents

```text
summary.json       complete machine-readable summary
summary.csv        small table for spreadsheets
summary.md         small table for people
arms/              one complete JSON result per experiment variant
events/            ordered lifecycle and policy events
model-gateway/     model request and response timing
policy-control/    Tool admission and completion records
runtime-traces/    Runtime and ClawTune records
tool-artifacts/    SSH, cgroup, eBPF, and validation data
model-traces/      model responses recorded during API runs
owned-sandboxes.jsonl  VM ownership and cleanup journal
```

A missing directory in a failed run is evidence of where the run stopped. Do
not copy replacement data from another run.

## 1. Check correctness first

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  concurrency: .arm.concurrency,
  status,
  completed: .correctness.completed_sessions,
  failed: .correctness.failed_sessions,
  validation: .correctness.validation_passed,
  telemetry_join: .correctness.native_tool_exact_id_join_rate,
  telemetry_loss: .correctness.native_tool_telemetry_loss_total,
  host_oom: .memory.host_oom_kill_events,
  safety_events: .performance.admission_control.safety_intervention_count
}' summary.json
```

A usable result normally has all sessions completed, validation passing,
telemetry join rate `1.0`, telemetry loss `0`, no host OOM, and no unexplained
safety event. Also confirm that the post-run CubeSandbox inventory contains no
VM owned by the run. Keep failed runs, but do not include them as successful
samples.

## 2. Read performance

| Field | Meaning |
| --- | --- |
| `agents_per_minute` | Correctly completed agents per minute |
| `steps_per_minute` | Completed model and Tool steps per minute |
| `jct_mean/p50/p90/p95_seconds` | Agent completion-time distribution |
| `tool_latency_*_seconds` | Native SSH Tool-command latency |
| `blocked_admission_seconds` | Sum of memory-wait time across commands; waits may overlap |
| `admission_control.wait_*` | Distribution of individual memory waits |
| `duration_seconds` | Complete experiment-variant wall time |
| `sandbox_create_mean_seconds` | Mean Runtime+Tool creation service time per agent |

Compare only variants using the same workload, templates, replay timing,
arrival schedule, random seed, memory pool, and host scope.

## 3. Read memory

| Field | Measurement |
| --- | --- |
| `memory.mean_used_delta_bytes` | Mean increase in host physical-memory use from the pre-variant baseline |
| `memory.peak_used_delta_bytes` | Peak increase in host physical-memory use |
| `memory.memory_time_integral_byte_seconds` | Host memory use integrated over time |
| `tool_execution_observations[].actual_measured_memory_mib` | Tool process memory measured inside the Tool VM |
| `actual_host_execution_increment_mib` | Host VM-memory increase around one Tool command |

Guest Tool memory is used to profile commands. Host memory is used to evaluate
VM density and checkpoint reclamation. These values are not interchangeable.

## 4. Read checkpoint and restore time

Arm-level totals are available in `summary.json`:

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  checkpoint_count: .performance.pause_count,
  checkpoint_total_s: .performance.pause_service_seconds,
  restore_count: .performance.resume_count,
  restore_total_s: .performance.resume_service_seconds,
  mean_host_bytes: .memory.mean_used_delta_bytes,
  peak_host_bytes: .memory.peak_used_delta_bytes
}' summary.json
```

For each Runtime or Tool VM operation:

```bash
jq '.arms[] | .performance.session_timelines[]
  | .lifecycle_timings[]
  | {role, operation, service_seconds, status, state_before, state_after,
     host_observed_reclaimed_bytes, host_reclamation_evidence}' summary.json
```

The operations are:

| Name | Timed work |
| --- | --- |
| `create` | Create and prepare one Runtime or Tool VM |
| `checkpoint` | Synchronous `sandbox.pause(wait=True)` call, including work CubeSandbox completes before returning |
| `restore` | Restore the VM and wait for CubeSandbox readiness |
| `destroy` | End-of-session VM cleanup |

Runtime and Tool records are separate. For one paired checkpoint, add the two
matching service times. `pause_count` and `resume_count` count these role-level
operations, not model requests.

`checkpoint.service_seconds` includes snapshot serialization and live-VM
eviction completed before the CubeSandbox call returns. It does not by itself
prove that host memory was reclaimed. Use
`host_observed_reclaimed_bytes`, `host_reclamation_evidence`, and the host
memory samples for that conclusion. Any asynchronous reclamation after the API
returns is outside the recorded service time.

Creation is reported separately from Agent completion time. Checkpoint and
restore delays that occur during workload execution affect completion time;
final destroy time is cleanup overhead.

## 5. Read P90 prediction results

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  predictions: .performance.prediction_observation_count,
  fallback_rate: .performance.prediction_fallback_rate,
  sources: .performance.prediction_source_distribution,
  fallback_levels: .performance.prediction_fallback_level_distribution,
  absolute_error_p90_mib: .performance.prediction_absolute_error_p90_mib,
  underestimate_p90_mib: .performance.prediction_underestimate_p90_mib,
  coverage: .performance.prediction_coverage_fraction,
  kb: .provenance.prediction_artifact
}' summary.json
```

A command-specific result should use the same frozen KB hash in every compared
variant and report its fallback rate. If most commands use a global fallback,
the result does not demonstrate command-specific P90 admission.

## Recommended reading order

1. Reject failed or incomplete variants using the correctness fields.
2. Compare throughput and Agent completion time.
3. Compare mean, peak, and time-integrated host memory.
4. Account for memory-wait, checkpoint, restore, and response-hold time.
5. For P90 policies, check prediction source, fallback rate, and error.
6. Confirm identical workload and environment provenance.

Keep the original YAML, source commits, template and guest-kernel identifiers,
trace hashes, KB hash, raw events, and validation output with the final report.
