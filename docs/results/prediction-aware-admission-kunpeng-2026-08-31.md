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
- Incremental command P90 cannot predict lazy first-touch materialization of
  the guest's fixed 2 GiB RAM mapping. A separate conservative materialization
  pool therefore admits at most
  `floor((16 GiB - 1 GiB headroom) / 2 GiB) = 7` potentially fully resident
  Tool VMs. This slot is held until VM close, verified checkpoint eviction, or
  verified balloon reclamation. It is not counted as a P90 reservation.
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

For rec-a's 31 concrete invocations, the frozen hierarchy selected 25
repository command-prefix nodes (14 depth-4, 10 depth-3, one depth-1), three
public tool-name nodes, and three public global fallbacks. Evidence counts were
one for 14 invocations, two for one, three for 10, and 58 for the six public
fallbacks. Thus this is genuinely per-tool and held out by recording set, but
many narrow nodes are statistically sparse; the coverage/error measurements
must accompany the throughput result and no cross-task generalization is
claimed.

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

### Whole-system boundary and Kubernetes decision

The experiment runner and the Kubernetes control plane are two backends of one
ClawBox system, not competing partial prototypes. They share workload inputs,
images, prediction artifacts, correctness contracts, and result-envelope
semantics. The direct backend owns the mechanism-specific path needed here:
atomic Runtime/Tool admission, explicit VMM snapshot ordering, live VMM RSS,
NUMA pinning, and balloon control. The Kubernetes backend remains the product
path for API persistence, RBAC/secrets, multi-tenant reconciliation, and normal
Cell lifecycle.

No completed experiment so far creates a reason to insert Kubernetes into the
paper execution loop. It would still require a Kata/containerd integration to
expose the same checkpoint and balloon operations and would add controller
state to a fail-stop, single-host experiment. Kubernetes becomes necessary if
the claim expands to long-running multi-node service recovery, HA, or shared
cluster policy enforcement; none of those are claimed by this evaluation.

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

The zero-wait, one-session balloon smoke completed 1/1 sessions with full
correctness and no failures. All 53 balloon target changes reached their guest
target; 26 tool-end reclamation events observed 47.4 MiB of aggregate
Firecracker RSS release. Peak measured Tool-Firecracker RSS was 349.4 MiB,
the maximum command working set was 1.465 MiB (257.465 MiB including the idle
anchor), and Tool RSS plus the 1 GiB headroom never exceeded the 16 GiB budget.
This validates lifecycle integration but is not a density result by itself.

The matched c=20 resident-balloon arm is a density result. It completed 20/20
sessions and 540/540 steps with full correctness, the same final-state hash,
zero failures/OOMs, and zero admission or `Tool RSS + headroom` violations.

| Resident policy | Correct | Wall s | Agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h | Materialization wait s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P90, no live reclaim | 20/20 | 612.7 | 1.958 | 52.88 | 6.53 / 8.82 | 1.115 | 1,225.4 |
| P90 + virtio-balloon | 20/20 | 467.9 | 2.565 | 69.24 | 8.92 / 11.85 | 1.168 | 163.0 |

Balloon reuse improved correct-agent throughput by 31.0%, reduced wall time by
23.6%, and cut materialization-slot queueing by 86.7%. It deliberately admitted
more resident pairs, so mean/peak RSS increased 36.6%/34.4%; the shorter run
kept RSS-time to a 4.8% increase. All 1,060 balloon changes reached target.
The 520 tool-end events observed 1.234 GiB of aggregate Firecracker RSS
decrease, took 142.0 s aggregate balloon service, and left each Tool
Firecracker at 93.5--326.0 MiB RSS. All therefore satisfy the post-run
fail-closed release check (guest target reached and host RSS below the measured
256 MiB idle anchor plus 50% margin, 384 MiB). Commit after the run enforces
that check online; the accepted run used `af31b15` and is not rerun.

This arm demonstrates elastic *physical residency*, not VM capacity resizing:
every Tool VM remained configured at 2 GiB. Traditional virtio-balloon is
enough for this baseline; virtio-mem hot-unplug would change virtual capacity
and add an orthogonal guest-cooperative asynchronous mechanism without solving
an unmet requirement in these results.

## Results

Results are accepted only from completed, internally valid arm summaries. The
static-control run and prediction-aware sweep use separate immutable output
directories and source manifests.

A final attempt to produce a corrected, same-commit fixed-2-GiB c=20 pair was
stopped at the user's reporting deadline. Its checkpoint arm had no completed
summary or correctness aggregate (sessions were at model steps 24--27), and
the resident arm had not started. `/data/static-fixed2-materialization-c20` is
therefore diagnostic-only and excluded from every table and effect estimate.
The checked-in study config is retained for a future completion. Consequently,
the accepted results support static capacity/checkpoint controls at c=8, the
checkpoint mechanism under c=20/c=40 long-wait pressure, and corrected P90
resident/checkpoint/balloon comparisons at c=20; they do not provide a fully
matched corrected static-vs-P90 full-trace factorial.

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

### Corrected full-trace c=20 P90 reservation

The accepted telemetry-enabled rec-a comparison uses fixed 2 GiB Tool VMs,
the 16 GiB Tool budget, 1 GiB safety headroom, and the independent 7-slot
fixed-capacity materialization guard. Both arms ran from commit `af31b15`, used
the same 27-step replay timing, images, repository commit, NUMA node, and
correctness oracle, and produced the same final-state hash.

| Policy | Correct | Wall s | Agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h | Materialization wait s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| P90 resident | 20/20 | 612.7 | 1.958 | 52.88 | 6.53 / 8.82 | 1.115 | 1,225.4 |
| P90 LLM-wait checkpoint | 20/20 | 1,043.5 | 1.150 | 31.05 | 9.32 / 15.07 | 2.691 | 3,062.2 |

Both arms completed 540/540 model steps with zero failures, OOMs, admission
budget violations, or `Tool RSS + headroom` violations. Fixed 2 GiB capacity
was sufficient: the maximum observed command peak was 2.930 MiB and the
maximum working-set estimate including the 256 MiB idle anchor was
258.930 MiB. Resident telemetry joined 247 command windows with 100% P90
coverage and +0.787 MiB mean prediction error (prediction minus actual).
Checkpoint telemetry joined 106 windows with 90.6% coverage and +0.868 MiB
mean error; the lower join count is reported rather than imputed.

Checkpointing was a negative result on this short-wait full trace: correct
throughput fell 41.3%, wall time increased 70.3%, mean/peak RSS increased
42.7%/70.9%, and RSS-time increased 141.4%. Its 540 cycles nevertheless all
verified zero Runtime+Tool Firecracker RSS after eviction and recorded
259.5 GiB of event-summed pair RSS release. The cost was 4,984.2 s aggregate
snapshot service and 54.3 s aggregate restore service (1,080 VM operations of
each kind), overlapped across sessions. Together with the positive 60-second
wait c=20/c=40 microbenchmark, this establishes a policy boundary: checkpoint
long LLM waits under admission pressure, but do not checkpoint every short
wait.

### Superseded full-trace c=20 diagnostic

An earlier full 27-step rec-a run used 20 resident 2 GiB Tool VMs.
Both arms used the live-RSS feedback gate, 16 GiB Tool budget, 1 GiB safety
headroom, identical images/timing/NUMA placement, and the same correctness and
final-state oracles. Static admission charged the remaining gap to each VM's
2 GiB capacity; prediction-aware admission charged the per-tool incremental
P90. All 20 sessions and all 540 model steps completed in each arm.

| Admission | Correct | Wall s | Agents/min | Steps/min | Mean/peak FC RSS GiB | RSS-time GiB-h | Aggregate / max Tool wait s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| static 2 GiB capacity charge | 20/20 | 614.6 | 1.953 | 52.72 | 17.09 / 25.15 | 3.668 | 6,455.0 / 55.65 |
| per-tool P90 reservation | 20/20 | 291.5 | 4.117 | 111.16 | 16.69 / 25.18 | 1.574 | 443.5 / 4.41 |

In that implementation, prediction-aware admission appeared to improve correct-agent and step throughput by
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
below budget according to the then-recorded charge.

This comparison is now excluded from the main claim. The old runner released
a worst-case VM-materialization commitment before lazy guest first-touch had
finished. A telemetry-enabled repeat showed all 20 Tool VMs could subsequently
materialize to roughly 20 GiB of aggregate RSS despite the 16 GiB Tool budget.
The command P90 gate itself was operating on its intended target, but it was
not a sufficient guard for fixed-capacity VM backing. The corrected runner
separates the 7-slot materialization pool from per-tool incremental P90 and
holds a slot until physical reclamation is verified.

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
[`p90-reservation-checkpoint-c20-study.json`](artifacts/kunpeng-2026-08-31/p90-reservation-checkpoint-c20-study.json),
with immutable remote output `/data/prc20-telemetry-fixed2-v4`. The resident
balloon extension uses
[`p90-balloon-c20-study.json`](artifacts/kunpeng-2026-08-31/p90-balloon-c20-study.json).
The older
[`p90-reservation-rec-a-c20-study.json`](artifacts/kunpeng-2026-08-31/p90-reservation-rec-a-c20-study.json)
and
[`static-fixed2-rec-a-c20-study.json`](artifacts/kunpeng-2026-08-31/static-fixed2-rec-a-c20-study.json)
are retained only to reproduce the superseded diagnostic above.
