# Experiment llc-c0-c2-pilot-c8-real-reverse-20261018

Run: `llc-c0-c2-pilot-c8-real-reverse-20261018-01`

Mechanisms are explicit per arm; a configured mechanism can have zero actions.

Fixed conditions: `{"prediction_source": "kb"}`.

| Arm | Admission | Runtime | Placement | Idle reclaim | Shared memory | Checkpoint | Deployment | Coordination |
|---|---|---|---|---|---|---|---|---|
| c0-existing-placement | predicted | memory=True cpu=True reclaim=True | load_aware affinity=False | wait/agent_and_tool | pressure | incremental-cow/warm-only/lru | managed | measured_cost |
| c2-adaptive-cluster | predicted | memory=True cpu=True reclaim=True | load_aware affinity=False | wait/agent_and_tool | pressure | incremental-cow/warm-only/lru | managed | measured_cost |

Resource scope: c8, 16 VMs, 48 GiB configured capacity; LOW/HIGH/HARD 384/448/928 GiB; 256 GiB shared pool; 64 GiB maximum live borrow.

Deployment and proactive memory-reclaim settings are listed per arm. Configured capacity is offered demand, not measured physical pressure.

| Policy | c | Status | Sessions | Success | Agents/min | JCT p50/p95/p99 s | CPU mean/peak cores | CPU throttled s | Admission blocked s | LOCAL peak GiB | Whole-host peak delta GiB | HIGH crossings | Above HIGH s/GiB-s | OOM kills |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| c0-existing-placement | 8 | succeeded | 8/8 | 1.00 | 0.72 | 661.46/662.96/663.29 | 7.29/16.71 | 0.00 | 2114.17 | 26.61 | 37.94 | 0 | 0.00/0.00 | 0 |
| c2-adaptive-cluster | 8 | succeeded | 8/8 | 1.00 | 0.68 | 697.38/699.95/700.30 | 7.23/16.68 | 0.00 | 2417.42 | 26.94 | 29.15 | 0 | 0.00/0.00 | 0 |

| Arm | Placement | Memory scheduling | Observed Tool peak/mean | Measured intervals | Offered-to-completion P50/P95/P99 s | Placement wait total s |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 7d094c7a098a88b3116d5db7 | load_aware | joint | 8.00/5.15 | 1416 | 662.19/663.60/663.82 | 0.75 |
| bf6916c556102d88f4bcb029 | load_aware | joint | 8.00/5.27 | 1416 | 698.42/700.79/701.29 | 0.74 |

Tool overlap includes admitted backend maintenance and uses completed operation intervals; `performance.observed_agent_tool_concurrency` separately measures Agent Tool operations. Cross-VM overlap requires synchronized Runtime VM clocks. Incomplete failed sessions are not evidence of zero Tool concurrency.

| Policy | Pause/restore count | Pause/restore s | Borrow count/s | Return/released count/s | Shared live/total peak GiB | WARM transferred GiB / private Delta GiB | Unique Base backing GiB/refs | WARM peak committed GiB | Prediction coverage | Prediction sources | Prediction error p90 MiB | ID join | Lost events | Cleanup |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| c0-existing-placement | 0/0 | 0.00/0.00 | 0/0.00 | 0/0/0.00 | 0.00/0.00 | 0.00/0.00 | 0.00/0 | 0.00 | n/a | non_command_fixed_host_budget:256, runtime_clawtune_immutable_kb:160 | n/a | 1.00 | 0 | True |
| c2-adaptive-cluster | 0/0 | 0.00/0.00 | 0/0.00 | 0/0/0.00 | 0.00/0.00 | 0.00/0.00 | 0.00/0 | 0.00 | n/a | non_command_fixed_host_budget:256, runtime_clawtune_immutable_kb:160 | n/a | 1.00 | 0 | True |

## Trial outcomes

Counts use all offered trials. OOM observations can overlap timeouts or other failures; missing counters remain unknown. Infrastructure or validation failures invalidate a performance comparison.

| Policy | Trials | Succeeded | Timed out | Failed | Cancelled / rejected | Observed OOM / unknown |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| c0-existing-placement / c=8 / sqlglot-4528 / explicit / 1a88939b | 1 | 1 | 0 | 0 | 0 | 0 / 0 |
| c2-adaptive-cluster / c=8 / sqlglot-4528 / explicit / 00945a0e | 1 | 1 | 0 | 0 | 0 | 0 / 0 |

## All-trial priority outcomes

Latency is conditional on completion. P95 requires at least 20 completed samples; its 95% bootstrap interval resamples whole trials and requires at least five trials. Failed, timed out and interrupted trials remain in the offered denominator. Completion counts include only verified session results; a killed worker may not report partial completions.

| Policy | Priority | Verified completed / offered | P95 s | 95% interval s |
| --- | --- | --- | ---: | --- |
| c0-existing-placement / c=8 / sqlglot-4528 / explicit / 1a88939b | high | 0 / 0 | n/a | n/a..n/a |
| c0-existing-placement / c=8 / sqlglot-4528 / explicit / 1a88939b | low | 0 / 0 | n/a | n/a..n/a |
| c2-adaptive-cluster / c=8 / sqlglot-4528 / explicit / 00945a0e | high | 0 / 0 | n/a | n/a..n/a |
| c2-adaptive-cluster / c=8 / sqlglot-4528 / explicit / 00945a0e | low | 0 / 0 | n/a | n/a..n/a |

## Runtime congestion control

| Arm | Backend | Activations | Max poll gap s | HIGH completed / offered | LOW completed / offered | HIGH completed samples | LOW completed samples |
| --- | --- | ---: | ---: | --- | --- | ---: | ---: |
| 7d094c7a098a88b3116d5db7 | explicit | n/a | n/a | 0 / 0 | 0 / 0 | 0 | 0 |
| bf6916c556102d88f4bcb029 | explicit | n/a | n/a | 0 / 0 | 0 / 0 | 0 | 0 |

Configured VM capacity, admission reservations, predictions, LOCAL physical use, live shared-NUMA borrowing, and WARM snapshot bytes are distinct quantities. Crossing HIGH is an observed control event, not a failed arm. Policy OOMs and timeouts are outcomes, not samples to discard. Crossing the shared-pool capacity, validation failures, telemetry loss, or unverified cleanup invalidate performance comparison. One repetition does not estimate run-to-run uncertainty.

Cross-NUMA traffic bytes: unavailable; no qualified platform-specific host uncore PMU event was configured. `numa_allocation_event_deltas` counts kernel allocation events in pages and is not a traffic-byte measurement.

## Compute nodes

Each node has independent admission and LOW/HIGH/HARD. Overshoot seconds in the aggregate table sum node-seconds. Node peaks include conservatively charged unattributed bytes; they must not be summed as a simultaneous physical peak.

| Arm | Node | NUMA | CPUs | LOW/HIGH/HARD GiB | Peak local GiB | HIGH crossings | Borrows | Verified placements |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 7d094c7a098a88b3116d5db7 | node0 | 0 | 0-79 | 192.00/224.00/464.00 | 10.94 | 0 | 0 | 8 |
| 7d094c7a098a88b3116d5db7 | node1 | 1 | 80-159 | 192.00/224.00/464.00 | 15.70 | 0 | 0 | 8 |
| bf6916c556102d88f4bcb029 | node0 | 0 | 0-79 | 192.00/224.00/464.00 | 8.96 | 0 | 0 | 6 |
| bf6916c556102d88f4bcb029 | node1 | 1 | 80-159 | 192.00/224.00/464.00 | 18.00 | 0 | 0 | 10 |

CPU/memory bindings are verified after VM creation and restore, before further worker dispatch. The supported priority-domain path prepares the cpuset before VMM allocation; check runtime_memory_placement events for preallocation enforcement and vCPU affinity. They do not prove that existing pages migrated. Per-NUMA physical samples are in memory-timeseries.csv; session placement and cgroup evidence are in each arm's JSON.

