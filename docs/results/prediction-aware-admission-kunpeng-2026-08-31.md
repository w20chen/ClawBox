# Prediction-aware Tool admission and LLM-wait checkpointing, Kunpeng, 2026-08-31

## Experimental semantics

This study uses the direct-Firecracker paper runner as a single-host,
NUMA-controlled, fail-stop experiment executor. It makes no Kubernetes,
high-availability, or cross-node recovery claim.

- Tool VM capacity is fixed for the lifetime of a VM. The predictive arms use
  2 GiB; Firecracker RAM is not resized between tool calls.
- A P90 reservation is per-tool predicted *incremental* demand used only by a
  shared NUMA-0 admission/accounting gate.
- Before a tool-bearing model response becomes visible to the Runtime VM, the
  runner requires `live Tool-Firecracker RSS + outstanding incremental
  commitments + new incremental P90 + safety headroom <= budget`. The study
  uses a 16 GiB controlled Tool-memory budget and 1 GiB safety headroom. If the
  inequality fails, the response remains queued.
- A 100 ms host sampler feeds actual Tool-Firecracker RSS growth into the gate.
  Prediction overruns therefore reduce remaining headroom and block later
  admissions. The next distinct model request is the tool-completion signal;
  it triggers a fresh RSS measurement before the future-growth commitment is
  removed. Persistent resident pages remain charged.
- LLM-wait checkpointing is independent of reservation accounting. It saves and
  evicts both sandboxes during inference waits. Each event verifies that both
  Firecracker process RSS values reached zero and records cgroup/NUMA change.
- Actual command working set is joined from the Tool VM's per-execution cgroup
  records. It is used for prediction-error, coverage, oracle-opportunity, and
  fixed-capacity-sufficiency analysis, never as an online admission input.

The prediction hierarchy is exact command/template, then program, tool, and
global fallback. The immutable plan SHA-256 is
`aeb621756df1bd014c1ea6ea53ef68279f9167a875fddfde45ca271ec52a3b8c`.
Its capacity check uses a measured idle Tool-Firecracker RSS anchor
of 256 MiB plus a 25% margin. Admission does not use that idle floor because
live RSS already contains it. After 25% command headroom, rec-a and rec-b
contain four distinct incremental P90 values from 1,845 to 1,870 KiB
(approximately 1.802--1.826 MiB). All predictive invocations select the fixed
2 GiB VM size class.

## Design

The factorial arms at each concurrency are:

| Admission policy | Tool VM capacity | Sandbox policy |
| --- | ---: | --- |
| Static | 4 GiB | resident |
| Static | 4 GiB | LLM-wait checkpoint |
| Static | 2 GiB | resident |
| Static | 2 GiB | LLM-wait checkpoint |
| Per-tool P90 reservation | 2 GiB | resident |
| Per-tool P90 reservation | 2 GiB | LLM-wait checkpoint |

The requested density sweep is `[20, 40]` on NUMA node 0 with
exclusive Runtime/Tool vCPU pairs. Both replay traces use the same task,
prompt, rootfs images, recorded timing, pinned repository commit, final-state
validator, and full repository correctness command. The two traces are
trajectories of one independent SWE task, so cross-task confidence intervals
are not estimable.

Primary reporting fields are correct agent runs/min, model steps/min,
reservation queueing, predicted and actual command memory, peak/mean
Firecracker RSS, Firecracker RSS-time, NUMA/cgroup memory, checkpoint-reclaimed
memory, checkpoint save/restore service time, OOM/failures, and correctness.

### Resident live-reclamation extension

The separate resident-balloon arm keeps the same fixed 2 GiB Tool-VM capacity
and per-tool P90 admission. It adds a pre-boot virtio-balloon device with
`deflate_on_oom=true`: `tool_start` deflates the balloon to zero, and
`tool_end` inflates it to 1,728 MiB, leaving the measured 320 MiB idle floor
available while keeping the VM alive. Each transition records target/actual
balloon size, service time, and before/after Tool-Firecracker RSS. Correctness
and host RSS, rather than guest-reported statistics alone, remain authoritative.

The installed Firecracker is v1.12.1. It supports the traditional live balloon
target API and statistics, and the 6.18.28 guest kernel has
`CONFIG_MEMORY_BALLOON`, `CONFIG_VIRTIO_BALLOON`, and `CONFIG_PAGE_REPORTING`.
This Firecracker version does not expose the newer free-page-reporting API, so
the first arm uses traditional balloon inflation. A future binary-upgrade arm
may enable continuous free-page reporting; virtio-mem hot-unplug is not part of
the main experiment.

## Results

Results are accepted only from completed, internally valid arm summaries. The
static-control run and prediction-aware sweep use separate immutable output
directories and source manifests.

The single-agent timing reference is the original recorded model time rather
than a c=1 replay experiment: rec-a contains 27 model steps and 136.624 s of
recorded LLM time; rec-b contains 39 steps and 170.131 s.

The first completed static c=8 dataset predates live-RSS admission and is kept
as a standalone capacity/checkpoint control. Its 2 GiB arms are static, not
predictive. All 64 sessions passed correctness, all 2,112 model steps
completed, both blocks had one final-state hash, and there were zero failures
or OOMs.

| Trace | Tool capacity | Policy | Correct | Agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| rec-a | 4 GiB | resident | 8/8 | 2.118 | 57.2 | 8.34 / 10.89 | 0.527 |
| rec-a | 2 GiB | resident | 8/8 | 2.124 | 57.3 | 7.50 / 10.06 | 0.473 |
| rec-a | 4 GiB | checkpoint | 8/8 | 0.764 | 20.6 | 15.91 / 32.02 | 2.769 |
| rec-a | 2 GiB | checkpoint | 8/8 | 1.089 | 29.4 | 8.51 / 16.38 | 1.039 |
| rec-b | 4 GiB | resident | 8/8 | 1.784 | 69.6 | 8.57 / 10.82 | 0.649 |
| rec-b | 2 GiB | resident | 8/8 | 1.793 | 69.9 | 7.88 / 10.01 | 0.594 |
| rec-b | 4 GiB | checkpoint | 8/8 | 0.534 | 20.8 | 15.48 / 32.01 | 3.836 |
| rec-b | 2 GiB | checkpoint | 8/8 | 0.787 | 30.7 | 9.14 / 16.65 | 1.541 |

At c=8 the 2 GiB resident control preserved throughput while reducing mean RSS
by 0.84 GiB on rec-a and 0.69 GiB on rec-b. Full checkpointing was slower and
increased aggregate RSS-time because snapshot I/O and restored memory mappings
dominated these short traces. This is a negative result for checkpointing at
this concurrency, not evidence of reclamation benefit.

### Controlled checkpoint-density result

A separate c=20 mechanism trace uses three 60-second model waits, two no-op Tool
calls, fixed 2 GiB Tool capacity, and a 40 GiB Runtime+Tool resident-pair budget
that admits 10 of 20 pairs at once. It is intentionally a mechanism
microbenchmark, not an additional SWE task result. Both arms ran the real
OpenClaw/Tool loop and full repository correctness command.

| Policy | Correct | Wall s | Correct agents/min | Mean/peak FC RSS GiB | RSS-time GiB-h |
| --- | ---: | ---: | ---: | ---: | ---: |
| resident | 20/20 | 464.2 | 2.585 | 8.30 / 9.41 | 1.071 |
| checkpoint | 20/20 | 282.6 | 4.246 | 4.03 / 20.25 | 0.316 |

Checkpointing improved correct-agent throughput by 64.3%, reduced wall time by
39.1%, and reduced Firecracker RSS-time by 70.4%. All 60 checkpoint events
verified both Firecracker process RSS values reached zero; summed released pair
RSS was 38.54 GiB. Final state matched and there were zero failures. Concurrent
restore increased peak RSS by 115%, so the result demonstrates a throughput and
memory-time benefit under admission pressure, not a peak-memory benefit.

The matched c=40 repeat increases pressure while retaining the same trace,
fixed 2 GiB Tool capacity, and 10-pair resident limit:

| Policy | Correct | Wall s | Correct agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| resident | 40/40 | 910.4 | 2.636 | 7.909 | 8.38 / 9.37 | 2.120 |
| checkpoint | 40/40 | 375.1 | 6.398 | 19.195 | 7.60 / 20.22 | 0.793 |

At c=40 checkpointing improved correct-agent throughput by 142.7%, reduced
wall time by 58.8%, and reduced Firecracker RSS-time by 62.6%. All 120
checkpoint cycles verified zero pair-process RSS after eviction, with 75.73
GiB of event-summed pair RSS released. Both arms produced equal final state,
zero failures, and no leaked admission leases. Restore bursts again raised
peak RSS (115.7%); mean RSS fell 9.3%. The aggregate snapshot and restore
service times were 1,048.8 s and 11.8 s respectively, overlapped across the
40 sessions.

### Full-trace c=20 prediction-aware admission

The full 27-step rec-a trace was then run with 20 resident 2 GiB Tool VMs.
Both arms used the live-RSS feedback gate, 16 GiB Tool budget, 1 GiB safety
headroom, identical images/timing/NUMA placement, and the same correctness and
final-state oracles. Static admission charged the remaining gap to each VM's
2 GiB capacity; prediction-aware admission charged the per-tool incremental
P90. All 20 sessions and all 540 model steps completed in each arm.

| Admission | Correct | Wall s | Agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h | Aggregate / max Tool wait s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static 2 GiB capacity charge | 20/20 | 614.6 | 1.953 | 52.72 | 17.09 / 25.15 | 3.668 | 6,455.0 / 55.65 |
| per-tool P90 reservation | 20/20 | 291.5 | 4.117 | 111.16 | 16.69 / 25.18 | 1.574 | 443.5 / 4.41 |

Prediction-aware admission improved correct-agent and step throughput by
110.8%, reduced wall time by 52.6%, reduced Firecracker RSS-time by 57.1%,
and reduced aggregate Tool-admission wait by 93.1%. Mean Firecracker RSS fell
2.4%; peak RSS was effectively unchanged. There were zero failures, OOMs,
over-budget observations, or leaked leases, and final state matched within
each study.

The predictive arm made 520 gated Tool admissions using four distinct
reservations: 1.802, 1.812, 1.821, and 1.826 MiB. Its peak live-RSS admission
charge was 7.98 GiB, versus 15.84 GiB for static admission. Runtime feedback
observed growth beyond the small incremental prediction in 238 leases and
automatically reduced later headroom; the total charge nevertheless remained
below budget.

Guest cgroup working-set telemetry was unavailable in this image
(`guest collector helper is not configured`), so per-command cgroup prediction
error, formal coverage, oracle opportunity, and spare capacity below 2 GiB are
not reported. The bridge's process `max_rss` fallback recorded a 1,487 KiB
mean and 1,496 KiB maximum across 560 runtime-envelope commands, but this is
not substituted for the missing cgroup metric. Operationally, fixed 2 GiB was
sufficient for this held-out run because all 40 arm-sessions completed without
OOM and passed correctness; the safety margin cannot be quantified from this
run.

An earlier `/data/pr20` attempt is excluded: a replay SSE parser merged
multiple unindexed Tool calls and stopped after two steps. Commit `ead7e16`
fixes the parser and makes gateway errors or incomplete traces fail the arm;
the accepted predictive result is `/data/pr20-fixed`. The matched static result
is `/data/sf20-fixed` at `a8e5346`, whose only change from `ead7e16` is adding
the static study configuration; experiment runner code is identical.

## Reproduction

The static c=8 control suite is
[`two-hour-direct-c08-suite.json`](artifacts/kunpeng-2026-08-31/two-hour-direct-c08-suite.json).
The prediction-aware sweep is
[`prediction-aware-sweep-suite.json`](artifacts/kunpeng-2026-08-31/prediction-aware-sweep-suite.json).
The accepted full-trace c=20 arms use
[`p90-reservation-rec-a-c20-study.json`](artifacts/kunpeng-2026-08-31/p90-reservation-rec-a-c20-study.json)
and
[`static-fixed2-rec-a-c20-study.json`](artifacts/kunpeng-2026-08-31/static-fixed2-rec-a-c20-study.json).
