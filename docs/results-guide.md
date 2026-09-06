# Reading ClawBox experiment results

ClawBox produces one result directory per run. The directory is the evidence
bundle; `summary.md` is only a convenient index and does not replace the raw
JSONL and per-arm records.

## Get a result bundle

When an experiment runs locally, the bundle is `<output-root>/<run-id>/`.
The CLI prints this path after `experiment run`. For a completed run:

```bash
.venv/bin/python -m clawbox.cli --output-root /data/clawbox-results \
  experiment status <run-id>
.venv/bin/python -m clawbox.cli --output-root /data/clawbox-results \
  experiment collect <run-id> > /data/clawbox-results/<run-id>.collect.json
```

To copy a Kunpeng result, copy the whole directory:

```bash
scp -r kunpeng:/home/weitianc/clawbox-results-current/<run-id> ./results/
```

For large runs, archive the directory on Kunpeng first and copy the archive.
Keep its SHA-256 beside it. Do not rename individual arm files: their arm ID is
the join key used by summaries and provenance.

## What is in the directory?

```text
summary.json       machine-readable run summary, all arms
summary.csv        compact arm table for spreadsheets
summary.md         human-readable arm table
arms/<arm-id>.json complete ResultEnvelope for one policy/concurrency arm
events/<arm-id>.jsonl ordered lifecycle and decision events
model-gateway/     model requests, waits, holds, and response delivery
policy-control/    admission/completion records
runtime-traces/    Runtime/OpenClaw and ClawTune traces
tool-artifacts/    SSH bridge, cgroup-v2, eBPF, and validation artifacts
model-traces/      API-mode traces that can be frozen for replay
owned-sandboxes.jsonl creation/cleanup ownership journal
```

Some directories are absent for a failed arm or compatibility driver. That is
evidence; do not silently fill them from another run.

## First-pass validity gate

Inspect every arm in `summary.json`:

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  concurrency: .arm.concurrency,
  status,
  completed: .correctness.completed_sessions,
  failed: .correctness.failed_sessions,
  validation: .correctness.validation_passed,
  join: .correctness.native_tool_exact_id_join_rate,
  telemetry_loss: .correctness.native_tool_telemetry_loss_total,
  oom: .memory.host_oom_kill_events,
  safety: .performance.admission_control.safety_intervention_count
}' summary.json
```

A successful paper arm normally requires `status: succeeded`, all sessions
completed, validation true, exact Tool telemetry join `1.0`, telemetry loss
`0`, host OOM `0`, and no unexplained safety intervention. Also inspect the
ownership journal and post-run CubeSandbox inventory for zero owned sandboxes.
A failed arm is retained as rejection evidence, not omitted from an average.

## Performance

Compare arms only when workload, workspace, template/image/kernel provenance,
NUMA/resource scope, memory budget, replay timing, seed, and session-to-trace
assignment are identical.

| Field | Meaning | Interpretation |
| --- | --- | --- |
| `agents_per_minute` | Valid Agents divided by workload window | Primary Agent throughput; higher is better. |
| `steps_per_minute` | Model + Tool steps per workload window | Useful for different-length trajectories. |
| `jct_mean/p50/p90/p95_seconds` | Agent completion distribution | Lower is better; p90/p95 show contention tails. |
| `tool_latency_*_seconds` | Native Tool operation latency | Separate SSH/tool delay from model wait. |
| `blocked_admission_seconds` | Sum of admission waits | May overlap across Agents; not wall-clock runtime. |
| `admission_control.wait_*` | Individual admission-wait distribution | Use p95 and queue depth to explain blocking. |
| `duration_seconds` | Arm wall-clock duration | Includes configured workload and lifecycle work. |
| `sandbox_create_mean_seconds` | Pair provisioning service time | Report separately from Agent JCT. |

Use `summary.json` and raw timelines for figures, not manually copied Markdown
values.

## Memory

Guest Tool memory and host VM memory are different measurements:

| Field | Scope | Use |
| --- | --- | --- |
| `memory.mean_used_delta_bytes` | Host-wide baseline-subtracted physical memory | Resident footprint and efficiency. |
| `memory.peak_used_delta_bytes` | Host-wide peak physical-memory delta | Safety and maximum density. |
| `memory.memory_time_integral_byte_seconds` | Host physical memory-time | Memory-time efficiency. |
| `tool_execution_observations[].actual_measured_memory_mib` | Tool guest cgroup RSS peak | Command profiling and prediction error. |
| `actual_host_execution_increment_mib` | Host VM RSS increment around one Tool call | Host-side admission calibration. |

Never substitute guest Tool RSS for host VM footprint. Snapshot should normally
reduce host mean/peak memory while adding pause/restore service time.

## Snapshots

Inspect pause/restore and host-memory fields with:

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  pauses: .performance.pause_count,
  restores: .performance.resume_count,
  pause_service_s: .performance.pause_service_seconds,
  restore_service_s: .performance.resume_service_seconds,
  mean_host_bytes: .memory.mean_used_delta_bytes,
  peak_host_bytes: .memory.peak_used_delta_bytes
}' summary.json
```

The event/timeline sequence should be model wait → Tool idle → paired pause →
Runtime restore → response release → lazy Tool restore at admission. A
successful pause API response alone is not reclamation evidence; use host
memory samples and lifecycle `host_observed_reclaimed_bytes`.

`resident` is the no-reclamation baseline. A lower snapshot memory number with
different traces, compressed waits, or a different budget is not a policy
effect.

## P90 admission

Inspect prediction source, fallback, error, and KB provenance:

```bash
jq '.arms[] | {
  policy: .arm.policy.name,
  predictions: .performance.prediction_observation_count,
  fallback_rate: .performance.prediction_fallback_rate,
  sources: .performance.prediction_source_distribution,
  levels: .performance.prediction_fallback_level_distribution,
  abs_error_p90_mib: .performance.prediction_absolute_error_p90_mib,
  underestimate_p90_mib: .performance.prediction_underestimate_p90_mib,
  coverage: .performance.prediction_coverage_fraction,
  kb: .provenance.prediction_artifact
}' summary.json
```

For a defensible command-specific result, compared arms use the same immutable
KB hash, source identifies that KB, and fallback rate is reported. A low
average error does not rescue a run dominated by a global fallback.

## Reading a comparison

1. Reject invalid arms using the correctness gate.
2. Compare throughput and JCT on the same valid Agent set.
3. Compare host mean/peak and memory-time; then explain guest Tool RSS.
4. Account for snapshot pause/restore service and response hold.
5. Report P90 source, fallback, error, and reservation accuracy.
6. Check provenance hashes, trace assignment, resource scope, and raw paths.

Typical interpretations are:

- resident is faster but consumes more host memory;
- snapshot uses less host memory but can worsen JCT through lifecycle service;
- P90 reduces blocking but underestimation or safety interventions invalidate a
  formal claim until KB/calibration improves;
- high throughput with invalid joins, missing eBPF, wrong identity, OOM, or
  leaked sandboxes is infrastructure output, not a successful Agent result.

For formal reports, retain the raw bundle, experiment YAML, commits,
template/image/kernel provenance, trace hashes, KB hash, and summary hash.
