# P90 sizing and LLM-wait checkpoint evaluation, Kunpeng, 2026-08-31

## Registered protocol

- Host: ARM64 Kunpeng, 320 logical CPUs, four NUMA nodes.
- Experimental locality: NUMA node 0 only, CPUs `0-79`, 515,829 MiB local
  memory; one pinned Runtime vCPU and one pinned Tool vCPU per task.
- Workload: SWE-ReBench task `15five/scim2-filter-parser#13`, Tool repository
  base commit `08c32462831d3849a70241ac9fea946b6b1884a6`.
- Runtime rootfs SHA-256:
  `826838d8b52b08b43da1e7fd90a50f1a33f8c29bf4a158c7992f8da1c779971c`.
- Tool rootfs SHA-256:
  `ef97a05974e4f2901bad6b00f9ecc8b5ab43b6a251c5d50389de6f82a687fdc4`.
- Prompt SHA-256:
  `7582a0d3d57943c761972a45d15346bfd61818e237c948b390ddbaec4367b0ea`.
- Main exclusive-CPU sweep: concurrency `[1, 8, 20, 40]`, three
  repetitions, recorded latency at `time_scale=1.0`.
- Six-arm factorial: conservative fixed 4 GiB, untrained fixed 2 GiB control,
  or frozen P90/floor 2 GiB Tool memory, crossed with resident or
  Runtime+Tool LLM-wait checkpointing. The fixed 2 GiB control is required
  because the prediction is safety-floor dominated.
- The suite parent, gateways, helpers, and Firecracker children are launched
  under `numactl --cpunodebind=0 --membind=0`; preflight rejects a parent that
  is not bound to NUMA node 0.
- Arm order is deterministically randomized within every
  workload/concurrency block. Blocks receive distinct derived seeds.
- Report task throughput and model-step throughput, Firecracker RSS, RSS-time,
  NUMA-local memory, cgroup memory delta, checkpoint counts, and save/restore
  time. Confidence intervals use Student's t and independent SWE task IDs as
  the unit. Repeated model trajectories are averaged within their task; with
  this data `n=1`, so a cross-task confidence interval is not estimable.
- Primary outcomes are completed-agent-runs/min and Firecracker RSS-time.
  Steps/min and failures/OOM are primary supporting outcomes; peak/P95 RSS,
  NUMA observations, cgroup delta, and save/restore service time are secondary.
  Primary comparisons are paired within repetition: P90 versus fixed 4 GiB,
  fixed 2 GiB versus fixed 4 GiB, P90 versus its same-size fixed control,
  checkpoint versus resident, and the sizing-by-checkpoint interaction.

## Prediction training and recording-set holdout

The frozen prediction was trained only on recording set 2. Exact
span/bridge/cgroup joins yielded 58 eligible completed calls across three
sessions (13, 14, and 31). The cgroup directories contained 99, 95, and 156
valid artifacts; 32, 32, and 24 recovered files were zero-length or all-NUL
after an unclean ext4 shutdown and were excluded. Exact span-to-bridge join
rates were 0.8333, 0.8696, and 0.8065; unmatched artifacts never trained.

The replay evaluation uses only recording set 1. This is a recording-set
holdout, not a task-level holdout: both sets come from the same SWE task.

| Trace | Model steps | Recorded LLM time | SHA-256 |
| --- | ---: | ---: | --- |
| rec-a | 27 | 136.624 s | `8be4a7a6affe1f316e585cf7bdef170cd6e48ba43bf844a923045a1793d28b47` |
| rec-b | 39 | 170.131 s | `0489dc897444bf1b26175f76ffbf69541ebca6a2f0e791a1168e8c8d89b9e801` |
| rec-c | 37 | 146.263 s | `38f95c281e3d1884bd52802fb4d80956ed4b2600269a495beea1f6243de37ddb` |

The immutable prediction has source digest
`da53b4317caa5183411e757d2f536c704e11b7e75ebadfeae0771c1f70a26497`,
pair digest
`ddda46de2518a20934cadba240a5f587c2c5cde985d1f1ae0f52e75ed674a17d`,
and ClawTune revision `76eab6fa5c6333f4e80901c030f10cab0e4ce605`.
Its repo-level `exec` estimates are 9.054 s latency P90, 0.900 cores CPU P90,
and 1,531,904 bytes memory P90 from 51 matching `exec` observations. The 2 GiB
safety floor therefore dominates the selected Tool VM size; this experiment
tests an evidence-gated reduced static size, not fine-grained per-call VM
resizing.

Leave-one-recording-out calibration on the 58 trusted recording-set-2 `exec`
observations produced the following descriptive coverage. Each row trains on
the other two recordings and predicts the held-out recording; the pooled row
is descriptive only because all recordings are trajectories of one task.

| Held-out recording | Trusted calls | Latency P90 coverage | CPU P90 coverage | Memory P90 coverage |
| --- | ---: | ---: | ---: | ---: |
| rec-a | 13 | 84.6% | 100.0% | 100.0% |
| rec-b | 14 | 92.9% | 85.7% | 100.0% |
| rec-c | 31 | 93.5% | 83.9% | 100.0% |
| Pooled descriptive | 58 | 91.4% | 87.9% | 100.0% |

Latency is computed from the same span start/end timestamps used to fit the
ClawTune KB. A post-registration parser regression fix also converts the
`action_duration_ns` fallback to seconds when an optional `duration_sec` field
is absent; that fix does not change the frozen main-suite source or any applied
VM sizing decision.

The raw spans identify the instrumentation repository field as `openclaw`.
Training therefore requires the explicit recorded mapping
`--observed-repo-fingerprint openclaw --repository
15five/scim2-filter-parser`; it is never silently relabeled. The memory signal
is per-execution command-cgroup memory, not whole-Tool-VM demand. Direct
Firecracker applies only the predicted Tool-memory decision; CPU remains one
pinned Tool vCPU in every arm.

## Checkpoint-density extension

The density suite uses a static 2 GiB Tool VM plus a 2 GiB Runtime VM and fixes
the configured resident admission budget at 160 GiB (40 VM pairs). It crosses
resident and LLM-wait checkpoint residency at requested concurrency
`[40, 60, 76]`, three repetitions, on NUMA node 0. Resident runs above 40 queue
in waves. Checkpoint runs release FIFO admission slots only after both VMs have
been saved and evicted, allowing another session to boot during the recorded
model wait; restore atomically reacquires one lease that owns both the
configured 4 GiB pair budget and a unique Runtime/Tool CPU pair. This prevents
an admitted session from colliding with another admitted session's static CPU
assignment. This tests achieved throughput
under a configured memory budget, not merely a theoretical density computed
from RSS. Concurrency 60/76 is intentionally CPU-oversubscribed and must be
reported as such.

Every completed density session also runs the repository's full correctness
command (`156` tests in the recorded trajectory's pinned test environment).
Report both completed sessions/min and correct completions/min, plus p50/p95/p99
session latency and p95/max admission wait. Snapshot allocated blocks quantify
storage amplification. The 160 GiB limit is an imposed production-style slice
of the 515 GiB NUMA node, not the node's physical capacity limit.

## Validity notes

- The three evaluation traces are model trajectories for one task and
  repository, not three distinct tasks. Results support within-task trajectory
  robustness only; cross-task generality requires another task set.
- P90 and the naive `fixed2` control both resolve to exactly 2 GiB. Their
  comparison is an implementation sanity check. Any advantage over fixed 4 GiB
  is attributable to selecting the smaller static size; this task does not
  demonstrate incremental predictive value.
- The fitted memory target is per-command cgroup RSS, not whole-Tool-VM demand.
  The conservative 2 GiB floor absorbs guest OS and ambient demand. Claims of
  calibrated fine-grained VM sizing require future whole-VM training telemetry.
- These legacy model traces preserve responses and measured latency but not
  request payloads, so request-by-request equality cannot be checked. The real
  OpenClaw/tool loop still runs, and the pinned-base Git patch plus non-ignored
  task files must be identical across arms. New recordings now include request
  payloads and fail replay on request divergence.
- Two preliminary pilots are excluded. The first validator included transient
  caches; the second included `.clawbox` telemetry and compared against a
  moving `HEAD`. One second-pilot arm also hit a torn serial-log JSON read. The
  corrected runner preserves validation output and tolerates incomplete log
  tails before the registered suite starts.
- A seven-arm partial launch is excluded at
  `/data/clawbox-paper-suite-001-excluded-unbound-20260831`: its Firecracker
  children were NUMA-local, but the suite parent/helpers were not bound. It was
  stopped before relaunching the registered run and is never pooled.
- A second five-summary partial launch is excluded at
  `/data/clawbox-paper-suite-001-excluded-sunlen-20260831`. The initial
  `fixed_control` label made the Runtime API socket path 110 bytes, beyond the
  107-byte Unix-domain limit, so those control VMs could not boot. The shorter
  output label `fixed2` and a fail-fast path-length regression check are used
  for the registered relaunch; no timing from the failed launch is pooled.
- A later 19-summary partial launch is excluded at
  `/data/clawbox-paper-suite-001-excluded-checkpoint-order-20260831`. Its first
  concurrency-8 checkpoint arm completed all eight sessions but split final
  state 4/4 between the intended patch and an empty diff. The runner had
  checkpointed Tool before Runtime; under concurrent snapshot I/O, a model
  response could reach the still-running Runtime while Tool was paused or
  evicted. The corrected dependency order checkpoints Runtime before Tool and
  restores Tool before Runtime. A concurrency-8 checkpoint gate must reproduce
  one final-state hash before the registered suite is relaunched. No timing
  from this partial launch is pooled.
- `throughput_tasks_per_minute` is a compatibility field counting completed
  agent sessions. It is not an official SWE-ReBench correctness result. The
  final-state validator is included in wall time and establishes equivalence
  across arms, not solution correctness.
- One checkpoint cycle saves and restores two VMs. Results separately report
  paired cycles, individual VM save/restore operations, summed VM service time,
  and paired wall-time effects.

## Results

Results are written only from `/data/clawbox-paper-suite-001/suite-summary.json`
and `/data/clawbox-paper-suite-001/measurements.csv` after the registered suite
completes, plus `/data/clawbox-paper-checkpoint-density-001` for the admission
budget experiment. No pilot or excluded-launch timing is included. The exact
registered inputs are tracked beside this report:

- [`main-suite.json`](artifacts/kunpeng-2026-08-31/main-suite.json)
- [`checkpoint-density-suite.json`](artifacts/kunpeng-2026-08-31/checkpoint-density-suite.json)
- [`p90-recording2-train-recording1-eval.json`](artifacts/kunpeng-2026-08-31/p90-recording2-train-recording1-eval.json)
- [`p90-loo-calibration.json`](artifacts/kunpeng-2026-08-31/p90-loo-calibration.json)
