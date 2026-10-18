# ClawBox c320/c640 real-trace evaluation tracking plan

Last updated: 2026-10-18 (Asia/Shanghai)

## Latest supplement execution (2026-10-18)

The user requested a smaller, evidence-based follow-up with carefully chosen
c320/c640 memory and pool sizes. See
[the new two-hour supplement plan](SUPPLEMENT-PLAN-20261018.md).
It supersedes the pending historical experiment matrix below: retain previous
results, prioritize fair Full/A-off/OS comparisons and actual-idle ablation,
and run at most six new arms with explicit tail-latency and time-budget gates.
The user subsequently authorized execution. See
[the supplementary result](SUPPLEMENT-RESULTS-20261018.md).

The planned c320 Full and A-off arms and three bounded Full calibration points
were executed on `kunpeng`. Every point had zero successful Agents in its
frozen observation window; runs that reached all 320 offered requests began
recording HTTP 408 failures. The planned point also had a 358.3 GiB LOCAL peak,
below its 384 GiB LOW watermark, with no OOM or memory PSI. Relaxing memory to
448/512 GiB and pacing arrivals at 1.5 or 2.5 seconds improved VM creation but
still produced zero completions and respectively 26 or 37 failures.

The matrix is therefore stopped rather than completed with uninterpretable
arms. OS, I-off, B-off, other secondary ablations, and c640 were not run:
there is no successful Full latency distribution to compare, relaxed Full did
not activate the memory mechanisms, and c320 did not meet the plan's condition
for escalating to c640. This is a configuration-search boundary, not a positive
or negative general claim about ClawBox. Further formal execution requires an
explicit choice to change execution capacity or the workload/window. Merely
prioritizing a small subset cannot satisfy the 95% completion gate because the
trace work count exceeds observed 960-second capacity. All attempted runs are
terminal with verified cleanup; no experiment
or QEMU process remains. The saved Kunpeng host profile now contains the M320
384/448 GiB, WARM 256 GiB, and borrowing 64 GiB settings.

## Historical stop and correctness verification

Formal experiments were initially stopped at the user's request on 2026-10-18;
the later supplement execution authorization superseded that instruction only
for the bounded work recorded above. Do not launch or resume additional
c320/c640 arms without a new user decision on the workload-capacity boundary.
The historical
experiment queue below is retained for reference, not authorization to run it.
The prepared `actual-idle-matrix/` configurations are not completed results.
`actual-idle-c320-full-01` never started: its prerequisite qualification
`qualification-fe2a68e8f908` failed on a policy-control port bind conflict before
session execution; its terminal state reports verified cleanup. No experiment
worker or launcher remained in the process inspection. Preserve all logs.

Repository verification completed: Windows 832 passed / 50 skipped; Kunpeng
isolated source with the actual CLI interpreter and saved ClawTune dependency
859 passed / 23 skipped. A small disabled-borrow candidate guard and stale test
fixtures/docs were corrected. No formal performance run was started.
See [verification evidence and limits](REPOSITORY-CORRECTNESS-20261018.md).
Formal experiments remain stopped; do not automatically resume the queue below.

## Objective and claim boundary

Evaluate ClawBox on one Kunpeng socket using CPUs 0-159, a 928 GiB LOCAL VM
budget, and a separate 384 GiB WARM/shared pool on NUMA node 2. The primary
question is whether the selected prediction-plus-admission ClawBox system
improves successful Agents/s over an OS-default deployment with no ClawBox
prediction, admission, placement, runtime control, reclamation, or borrowing.
Secondary mechanisms are evaluated only where they actually activate.

The controlled heterogeneous-memory trace is an operating-boundary experiment.
Its command-table prediction results cannot be presented as a formal ClawTune
KB or SWE-ReBench result. A selected real trace comparison is required before
making an application-level claim.

## Current correctness gate (2026-10-18, actual-idle repair)

The user authorized fixing the idle-checkpoint audit findings and explicitly
excluded LLM-duration prediction and oracle strategies. Do not launch more
Full/checkpoint/WARM-scaling arms until end-to-end verification below completes.
The OS-default screen has ended and its cleanup is verified.

**Gate cleared:** the repair is deployed and the bounded verification below is
complete for that bounded scope. The newer instruction above stops further
formal experiments. Overall experimental goals remain incomplete.

- Full and A-off at c640 LOW/HIGH 512/640 GiB, scale 0.75 have finished their
  1,200-second windows with verified cleanup. Both have zero successful Agents.
  A-off has 600 scheduled requests, 421 sessions with a VM created, 83 recorded
  independent failures, 4,873 agent-tool and 14,291 backend completions, LOCAL
  sampled peak 535.18 GiB, six borrows, zero checkpoint, and no OOM.
- OS-default `real-trace-p0-c640-balanced-os-s075-01` completed its matched
  screen: 598 sessions with a VM, zero successful Agents, 183 independent
  failures, 6,481 agent-tool completions, LOCAL peak 580.23 GiB, zero OOM.
  Preserve `raw-balanced-os.tgz` and the remote per-session/model logs.
- Confirmed design mismatch: `wait` required a 2.7181621785-second LLM-duration
  forecast and excluded candidates when that forecast expired. The expiry
  condition was added by commit d7a5126 on October 17 at 01:02 CST; frozen
  real-trace c320/c640 configs already contain the same forecast. Real LLM
  waits at 1:1 are present. Existing data describes this forecast-gated policy,
  not the intended actual-idle policy. Do not discard or silently relabel it.
- Deployed after OS cleanup: actual outstanding requests now define idle;
  release_cost and LLM-duration/oracle configuration are rejected. Admission,
  model-wait and watermark reclamation use the common selector. Both snapshot
  reservations precede either VM pause; WARM exhaustion causes backpressure.
  Cube-409 recovery honors pair scope. Response generation removes eligibility
  before waiting for committed checkpoint completion.
- Regression validation: 335 tests pass on Kunpeng Linux. Real VM qualification
  on NUMA 0 and 1 passed two-generation incremental pair checkpoint/restore,
  preserving Runtime PID and guest state. The c32 qualification at LOW/HIGH
  16/20 GiB completed 15 bounded sessions, including 12 paired pause/restore
  cycles with unchanged OpenClaw PIDs and no session/policy errors. It was
  stopped when retained file cache blocked further creation with proactive
  reclaim off; all logs and verified cleanup are retained. A subsequent c8
  public qualification with the existing cache-reclaim switch on completed
  8/8 and verified cleanup. Both retain 1:1 waits but only the first model step;
  neither is full-trace performance evidence. See `IDLE-CHECKPOINT-VERIFICATION.md`.
- The prior claim of 112 completed c320 pilot sessions is not yet supported by
  the raw run located as `real-trace-p0-c320-01`: its events contain zero
  session_complete, one session_failed, and LOCAL peak 225.35 GiB. Withdraw
  the 112-success claim unless a matching raw source is found; do not use it
  to select the next memory configuration.

The one-hour main-evidence target was not fully met: three matched observation
windows are complete, and the intended actual-idle checkpoint ablations
remain pending after this repair. Keep the overall goal incomplete.

## Status at a glance

| Priority | Run/evidence | Status | Decision |
|---|---|---|---|
| Prior boundary | c24/48 GiB predicted vs fixed-256 | COMPLETE | Predicted +66.5% successful throughput; motivates scale-up |
| No admission | c24/48 GiB attempts 01/02 | INVALID | Old drain/cleanup defect changed outcomes; do not claim a gain |
| VM supply | c320-01/02/03 and c1280-priority-2h-01 | EXCLUDED, CLEANED | Identified creation-path scaling defect and unsafe gate 64 |
| VM supply repair | create-path-smoke-c32-01 | COMPLETE, CLEANED | 64 VMs in 28.45 s; provisioning bottleneck removed |
| P0.1 | c1280-screen-c320-04 | INVALID, CLEANED | c320 VM supply passed, but FD exhaustion invalidated memory evidence |
| P0.1 | c1280-screen-c320-05 | INVALID, CLEANED | Predicted arm exposed the shared-return lifecycle race after 8,732 Tool operations |
| P0.1 | c1280-screen-c320-os-06 | COMPLETE, VALID TIMEOUT BOUNDARY, CLEANED | 0/320 Agents in 3,600 s; 9,152 completed operations (including backend maintenance); no OOM; peak measured VM-cgroup delta about 1,051 GiB |
| P0.1 | c1280-screen-c320-predicted-06 | COMPLETE, CLEANED | 320/320 Agents in 992.83 s, 19.47 Agents/min, offered P99 959.80 s, no OOM |
| P0.2 | c1280-primary-c640-07 | INVALID, DESTROYED | v5 omitted the configured completion marker; preserve only as diagnostic execution evidence |
| P0.2 | c640-v6-smoke-01 | COMPLETE, CLEANED | Real-VM 1/1 validation proves the corrected four-phase trace and frozen predictions |
| P0.2 | c1280-primary-c640-08-v6 | COMPLETE, CLEANED | Predicted 640/640 in 1,810.72 s; OS 0/640 inside 3,600 s and cleanup verified |
| Superseded | c320-admission-ablation-01 | CANCELLED BY USER, DESTROYED | Synthetic three-arm run stopped after about 14 minutes; completed-arm cleanup verified; no result is used |
| P0.3 | c1280, OS default vs predicted | BLOCKED ON STORAGE HEADROOM | Do not start while Cube storage can cross its 85% guard before intended memory contention |
| New matrix | real trace c320/c640 | c320 BOUNDARY RETAINED; c640 MATCHED A-OFF RUNNING | c640 scale=0.75 Full has a completed 20-minute observation window and verified cleanup. A-off runs the same configuration; OS default is queued after cleanup. Full matrix at a useful memory point remains pending |
| Pressure pilot | c320 HIGH=512 GiB | CANCELLED, CLEANED; COMPLETION CLAIM WITHDRAWN | Located raw run has 0 successful Agents, 1 failure, LOCAL peak 225.35 GiB and no pauses; prior 112-completion claim is unsupported |
| Pressure pilot | c320 HIGH=96 GiB | INVALID, CANCELLED, CLEANED | 133 pauses, 126 restores and 98 borrows, but restore churn caused failed Agents; retained only to bound the useful regime |
| Pair activation | c32 LOW/HIGH=48/52 GiB | PASSED, CANCELLED, CLEANED | 14 paired Runtime+Tool pauses and restores; 14 relay reconnects; zero policy failures; WARM peak 16.817 GiB |
| Formal attempt 01 | c320 P0 | INVALID, CANCELLED, CLEANED | NUMA migration exceeded a hidden 30 s limit and stopped the watermark controller; raw data retained only as diagnostic evidence |
| Formal attempt 02 | c320 P0 | INVALID, CANCELLED, CLEANED | Creation concurrency 16 caused 13 failures among the first 25 terminal Agents under concurrent trace/checkpoint load; fallback 8 selected before further time was spent |
| Formal attempt 03 | c320 P0 | INVALID, CANCELLED, CLEANED | At concurrency 8, three distinct pause/restore paths exposed coarse borrow reservations retained while Cube kept resumable tombstones populated |
| Formal attempt 04 | c320 P0 | INVALID, CANCELLED, CLEANED | Killing the populated cgroup after pause broke Cube tombstone restore semantics; the change was reverted. Pause now returns NUMA policy LOCAL while retaining physically backed residual reservation until restore/final destroy |

## Historical synthetic scale-up resource configuration

| Resource | Frozen value |
|---|---:|
| Compute CPUs | NUMA 0/1, CPUs 0-159 |
| LOCAL LOW / HIGH / hard | 800 / 864 / 928 GiB |
| WARM/shared pool | 384 GiB on NUMA 2 |
| Maximum live borrowing | 192 GiB |
| Combined live VM cgroup limit | 1120 GiB |
| Controller/support CPUs | CPUs 160-319 |
| VM creation concurrency | 16 pairs for the time-boxed primary run; 8 for the full follow-up if 16 causes creation failures |
| Arrival schedule | fixed stagger, 50 ms |
| Placement overcommit ceiling | 32x |
| Runtime/sample intervals | 2.0 / 1.0 s |
| Worker open-file soft limit | 65,536, applied from machine.env by the supported wrapper |

The socket has about 1007 GiB physical memory. LOCAL is capped at 928 GiB to
leave room for the current non-VM footprint and avoid turning a policy trial
into a host NUMA OOM experiment.

The new real-trace matrix keeps this physical hard cap but overrides the c320
LOW/HIGH policy watermarks as specified in its density table.

The applied `/sys/fs/cgroup/cube_sandbox/sandbox` parent was verified with
effective CPUs `0-159` and memory nodes `0-2`. Thus the OS-default arm inherits
the same 160-CPU ceiling while Linux manages the combined memory domain; it
cannot consume the controller CPUs `160-319`.

## Historical execution record and prior scale-up plan

### Execution rules after the VM-supply diagnosis

- Preserve every completed or already-excluded run; do not rerun it merely to
  apply the rules below. These rules apply only to trials that have not begun.
- Use the supported CLI `--detach` path only after validation and a clean
  predecessor gate. Keep the run-state file, launcher log and process IDs, and
  inspect early VM creation, stderr and policy events. Do not use a raw `nohup`
  command that bypasses these checks.
- One ClawBox Agent owns two VMs (one 4 GiB Tool and one 2 GiB Runtime).
  Therefore c320/c640/c1280 require 640/1,280/2,560 live sandboxes when the
  complete population is resident. Size host pools and report density in VMs,
  not only Agents.
- Before the next formal arm, run CubeSandbox `cube-bench` against each actual
  ClawBox template, both without and with its network policy. This separates
  raw CubeSandbox create latency from ClawBox cgroup, envd/SSH-readiness and
  two-VM orchestration overhead. Do not extrapolate the vendor's lightweight
  x86 result to this ARM64 host.
- Bound the public real-VM capability gate with
  `--qualification-concurrency 2`. The CLI default is the experiment's maximum
  concurrency and would otherwise duplicate every large arm before the formal
  run. This smoke validates paths and cleanup; only the formal arm supports
  density or performance claims.
- For the c1280 target, review and apply `tap_init_num >= 2560` (prefer 3,000 to
  match the existing cgroup pool and leave headroom), then verify the pool after
  Cubelet restart. This removes on-demand TAP creation from the formal path but
  is not treated as the explanation for the observed 40--80 s VM latency.
- Primary Tool-admission comparison: every arm starts from the same ready VM
  population and differs only in Tool dispatch/admission. Record provisioning
  separately; begin the policy measurement window only after all creation
  attempts are terminal, and reject the arm as infrastructure-invalid if the
  ready fraction is below the declared gate.
- Separate end-to-end deployment-admission comparison: offer the same Agent
  arrivals and allow each policy to decide how many Agent pairs become
  resident. Do not use this comparison alone to claim a Tool-admission gain.
- Derive each new provisioning deadline from an immediately preceding
  create-only calibration: `max(900 s, 1.5 * required_VM_count /
  sustained_successful_VM_per_second)`. The execution window is a separate
  field chosen from trace duration plus 25% headroom. Report provisioning,
  execution and finalization durations independently.

### P0.0 Two-hour primary pilot (excluded)

- Config: `.experiment-audit/c1280-design-20261017/priority-2h-c640-c1280.yaml`
- Run: `c1280-priority-2h-01`, launched by the maintained unattended script
  `run-priority-2h.sh`.
- Four fixed-window trials: c640 and c1280, each comparing
  `predicted-shared` with `os-default-no-admission`.
- Each arm has a 1200 s execution window and 300 s finalization ceiling. All
  offered Agents remain in the denominator; deadline-incomplete Agents are not
  presented as successes.
- Creation concurrency is 16. This is below the failed 64-pair pilot and above
  the stable but supply-limited 4-pair runs. Any creation errors are reported,
  not retried away.
- The frozen randomized order is c1280 OS, c640 OS, c640 predicted, c1280
  predicted. Qualification runs once before the detached supervisor starts.
- Status: CANCELLED AND EXCLUDED. Both qualification policies succeeded and cleaned up. The
  detached formal run `c1280-priority-2h-01` started at 2026-10-17 17:24
  Asia/Shanghai; supervisor PID 103791 is reparented to init. The first trial is
  c1280 OS/no-admission. At about 190 s it had 112 creation-gate acquisitions,
  191 created VMs, 559 completed Tool phases, one isolated Agent failure,
  about 194 GiB live VM-cgroup memory, and no OOM or current PSI. This is an
  early progress observation, not the arm result.
  At about 11.5 minutes, all 1280 Agents were queued, 183 had acquired the
  creation gate, 341 VMs had been created, and 5,199 backend Tool operations
  had completed. Live/peak VM-cgroup memory was about 514 GiB, average CPU was
  about 20.6 cores, PSI and OOM remained zero, and the isolated failure count
  remained one. Creation supply still limited realized density at that point.
  Final c1280 OS/no-admission result: `timed_out`, 0/1280 successful, 1280
  failed, cleanup verified. The 1200 s execution deadline reported 1279
  unfinished futures; the 300 s finalization window then cancelled and cleaned
  the remaining sessions. Peak VM-cgroup memory was about 639 GiB, with no OOM
  or memory PSI. This is an invalid primary throughput comparison and a valid
  supply/deadline boundary: the offered c1280 workload did not realize the
  intended 800--928 GiB LOCAL contention regime. The supervisor correctly
  continued to the c640 OS/no-admission trial instead of terminating the group.
  The c640 arm showed the same infeasible deadline immediately: after about
  150 s only 80/640 Agents had acquired the create gate and 143/1280 required
  VMs had been created. The run was therefore aborted through the public CLI;
  both started arms have verified cleanup. Do not use this pilot for a policy
  performance claim.

- Creation diagnosis: this was CubeSandbox lifecycle supply, not admission or
  host-memory saturation. Each Agent cold-creates a Tool VM and then a Runtime
  VM serially. At actual create-pair concurrency 4, the c320 pilot averaged
  6.47 s/Tool VM and 6.29 s/Runtime VM without creation failures. At concurrency
  16, the c1280 arm averaged 45.53 s and 41.63 s respectively; only 489 of the
  required 2,560 VMs existed at the 1,200 s execution deadline. Peak VM memory
  was about 639 GiB, average CPU about 20.6 cores, and memory PSI/OOM remained
  zero. Concurrency 64 was worse: about 78--82 s/VM and 55 creation failures
  (49 HTTP 408 and six envd resets/timeouts). Therefore do not raise the gate
  toward the offered Agent count.
- Measurement decision: report cold provisioning separately. A c640/c1280
  policy arm must not start its comparison window until the common required VM
  population is ready, or must use a uniformly reset warm pool shared by every
  baseline. Retain a smaller cold end-to-end case as an operating-boundary
  result. Before another large run, use a short create-only 4/8/12/16 sweep to
  select the host's saturation knee; do not infer it from physical CPU or RAM.
- Root cause and validated repair (2026-10-17): Cubelet and CubeAPI completed
  successful creates in roughly 0.2 s, but ClawBox then scanned every host
  process twice per VM to rediscover a cgroup whose secure preallocation path
  was already known. At hundreds of resident shims this became the observed
  40--140 s lifecycle delay. The supported path now verifies the populated
  preallocated leaf directly and passes that verified leaf to NUMA placement.
  In real-VM run `create-path-smoke-c32-01`, after removing the first scan, all
  64 VMs were created in a
  28.45 s window with no failures. Tool create mean/P95 was 6.19/8.36 s and
  Runtime create mean/P95 was 5.49/7.42 s, versus 45.53/110.76 s and
  41.63/114.80 s respectively in the excluded c1280 run. All 32 Agents and
  1,312 Tool operations completed, all 64 VMs were destroyed, and cleanup was
  verified. The callback forwarding the verified leaf, which removes the
  remaining borrower scan, was completed immediately after this run and passed
  the local and Kunpeng targeted tests; its timing is therefore not folded into
  the c32 numbers. An unrelated root workload also overlapped the later c32
  workload phase, so only the create-path result is retained. The run peaked at
  only 114.85 GiB LOCAL and is not admission-benefit evidence.
  The complete repair was then exercised by the first c320 arm: 640/640 VMs
  were created without errors in a 143.06 s window. Tool/Runtime create
  mean was 3.26/3.38 s and P95 was 7.23/7.41 s. This is the current
  provisioning calibration; policy performance is reported only after the arm
  reaches a terminal result.

### P0. Prediction/admission evidence

Prior controlled results define the operating boundary and motivate this
scale-up but do not replace it. At c8 with no memory pressure, predicted and
fixed differed by less than 3% (0.704 versus 0.685--0.698 Agents/min). At c28,
fixed-256 was better at both 80 GiB (7.25 versus 6.68 Agents/min) and 64 GiB
(9.35 versus 8.32 Agents/min); these are retained negative cases showing the
cost of unnecessary admission. At the tighter 48 GiB/c24 point,
`full-predicted` completed 24/24 in 295.21 s at 5.01 Agents/min, versus
`fixed-256` at 484.78 s and 3.01 Agents/min: a 66.5% successful-throughput
gain, with lower P95 JCT (256.18 versus 456.39 s), no OOM, and verified cleanup.
Two no-Tool-admission attempts reached the 48 GiB hard limit and failed for
policy-drain/cleanup infrastructure reasons (0/24 and 4/24 completed), not OOM.
They cannot support a no-admission comparison because the then-existing
failure propagation/cleanup defect changed the outcome. This is why the
repaired c320/c640 no-admission arms remain P0 rather than citing those failures
as a ClawBox benefit.

#### P0.1 Controlled c320 boundary

- Config: `.experiment-audit/c1280-design-20261017/c320.yaml`
- Excluded pilot: `c1280-screen-c320-01`. With creation concurrency 8, only
  174/320 sessions completed before the arm deadline and LOCAL peaked at about
  116 GiB, so VM supply, rather than memory contention, determined the result.
- Excluded pilot: `c1280-screen-c320-02`. It used the v3 trace but the caller's
  exported creation setting was superseded by the saved machine configuration,
  so the worker actually used concurrency 4. It was stopped without Agent
  failures after confirming the configuration mismatch.
- Excluded pilot: `c1280-screen-c320-03`. The machine overlay correctly applied
  creation concurrency 64, but that overloaded CubeSandbox VM creation: 55
  sessions failed (49 HTTP 408 plus six envd initialization resets/timeouts).
  Other Agents continued, confirming failure isolation. Cleanup required
  terminating orphaned shims belonging only to this run; it is now destroyed.
- Replacement run: `c1280-screen-c320-05` with creation concurrency 16 and the
  v3 trace. The trace reuses the seven independently trained commands, repeats
  the exact trained 2 GiB command four times, and interleaves it with the six
  128 MiB phases. No new prediction key is introduced.
- Primary arms:
  - `predicted-shared`: prediction-guided admission and the complete selected
    ClawBox runtime mechanisms.
  - `os-default-no-admission`: prediction, Tool/lifecycle admission, placement,
    runtime control, reclaim and borrowing disabled.
- The regenerated c320/c640/c1280 configs contain only these two primary arms.
  Fixed and single-mechanism admission ablations are deferred unless the main
  result requires attribution.
- Purpose: low-contention/control boundary and infrastructure progression test.
- `c1280-screen-c320-04` is infrastructure-invalid and cleaned. Its OS arm
  created all 640 VMs without errors in 143.06 s, so the repaired creation path
  is valid at c320. During execution, however, the worker's inherited soft
  limit of 1,024 file descriptors was exhausted by hundreds of concurrent VM
  samplers. The required node-memory sampler then could not read `/proc/meminfo`,
  so the worker correctly rejected the arm instead of publishing incomplete
  memory evidence. Four ordinary Agent failures remained isolated and cleanup
  removed all 640 VMs. The partial execution is diagnostic only: it is not a
  throughput comparison and predicted ClawBox did not run.
- `c1280-screen-c320-05` is ready with the same workload/resources and only the
  two primary arms. `machine-c1280.env` now saves
  `CLAWBOX_NOFILE_LIMIT=65536`; the supported wrapper validates it against the
  host hard limit and applies it on every invocation. This changes no policy,
  workload, memory budget, image, or prediction data.
- `c1280-screen-c320-05` subsequently ran the predicted arm for 2,123.09 s and
  completed 8,732 Tool operations before a shared-memory controller lifecycle
  race invalidated the arm. It had only two isolated Agent failures, no OOM,
  and no LOCAL high-watermark violation. The controller now observes return
  candidates through the lifecycle lock and records its underlying exception.
- `c1280-screen-c320-os-06` is the completed replacement OS/no-admission
  single-arm run. It used the frozen ten-phase c320 workload and reached the
  configured 3,600 s timeout with 0/320 completed Agents. The event log records
  9,152 completed operations across Agent Tool work and backend maintenance;
  2,171 Agent Tool requests were queued. There was no OOM kill or infrastructure
  failure. All 640 VMs were
  destroyed and cleanup was verified. The measured VM-cgroup peak delta was
  1,128,836,882,432 bytes (about 1,051 GiB). This is retained as the natural
  no-admission timeout boundary, not rerun as a successful workload.
- `c1280-screen-c320-predicted-06` completed all 320 Agents in 992.83 s
  (19.47 Agents/min) with no failed session, OOM or infrastructure failure and
  verified cleanup. Offered-to-completion P95/P99 were 947.61/959.80 s. Its
  measured VM-cgroup peak delta was 644,140,638,208 bytes (about 600 GiB),
  versus about 1,051 GiB for OS/no-admission. The shared-borrow mechanism did
  not activate, so this result supports the integrated prediction/admission
  path but is not evidence for shared borrowing.
- `continue-c640-after-c320.sh` waits for both c320 single-arm results and uses
  the supervisor's trial-outcome predicate. It starts `c1280-primary-c640-07`
  only when both results validate, contain no infrastructure failure, and have
  verified cleanup. OOM, timeout and isolated Agent failures remain valid
  outcomes rather than being retried until successful.
- Completion requirement: terminal result for both selected arms, verified cleanup, no
  arm-wide interruption caused by an ordinary Agent failure, and auditable
  started/resident/active counts.

#### P0.2 Controlled c640 primary contention point

- Config: `.experiment-audit/c1280-design-20261017/c640.yaml`
- `c1280-primary-c640-07` is excluded because the shortened v5 trace never
  wrote `/testbed/stress-result.txt`; 629 sessions reached validation but all
  necessarily failed that check. Its partial OS arm was cancelled and both
  ownership sets were cleaned before any replacement run.
- v6 preserves two 128 MiB and two 2,048 MiB phases and substitutes the third
  phase with the already-trained 128 MiB command that writes the completion
  marker. All four command hashes exist in the frozen prediction artifact.
  Trace SHA256 is
  `7c0e53b0b4ee6062b718e2187e46e30cc4902ccdcdc81c761f8770a6d6008119`.
- `c640-v6-smoke-01` completed 1/1 real Agent with validation and cleanup true,
  all four predictions from `frozen_clawbox_p50`, and no OOM or telemetry loss.
- `c1280-primary-c640-08-v6` predicted/admission completed 640/640 in
  1,810.72 s at 21.28 Agents/min, with offered P95/P99 1,763.88/1,768.68 s,
  JCT P95/P99 539.91/552.98 s, no failed session, OOM, telemetry loss or
  infrastructure failure, and verified cleanup. Peak measured VM-cgroup delta
  was 487,082,692,608 bytes. Shared borrow, return, pause and resume counts were
  all zero, so this arm supports the integrated prediction/admission path but
  does not support the dormant mechanisms individually.
- The OS/no-admission arm completed as a valid timeout boundary: 0/640 Agents
  completed inside the fixed 3,600 s execution window, 164 failed during
  creation and 476 futures remained unfinished at timeout. Three Agents passed
  validation only during post-deadline finalization and are correctly excluded
  from successful throughput. Peak measured VM-cgroup delta was
  873,553,260,544 bytes, with no OOM or infrastructure failure; all 1,280 VMs
  were destroyed and cleanup was verified. Of the isolated create failures,
  148 were CubeMaster `no more resource`. The `/data/cubelet` filesystem crossed
  the documented 85% storage guard during the arm and returned to 83% after VM
  cleanup. Therefore this is a valid platform-default end-to-end boundary, but
  it does not isolate memory contention from snapshot-storage capacity.
- Arms: `predicted-shared` and `os-default-no-admission` only.
- Purpose: main positive-gain candidate. The prior c24/48 GiB boundary scales
  to roughly c464 at 928 GiB, so c640 should enter the useful contention regime.
- Start gate: c320 cleanup is verified; at least 90% of offered sessions reach
  VM creation; controller/service overhead does not prevent Tool progress.
- Success evidence: successful Agents/s, success rate, all-offered P95/P99,
  admission wait, PSI, LOCAL/shared residency, OOM events, and control overhead.

#### P0.3 Controlled c1280 overload boundary (blocked, not scheduled)

- Config: `.experiment-audit/c1280-design-20261017/c1280.yaml`
- Status: BLOCKED by CubeSandbox storage headroom and superseded by the
  real-trace G1--G4 matrix below.
- Arms: `predicted-shared` and `os-default-no-admission` only.
- Purpose: determine sustainable throughput and failure behavior at the maximum
  offered concurrency, not claim 1280 simultaneous execution.
- Start gate: c640 reaches real resource contention and continues semantic
  progress; cleanup is verified. Prefer at least 120 busy VM CPU equivalents
  and either 700 GiB LOCAL use or sustained memory PSI.
- Required reporting: offered, VM-started, resident, checkpointed, active Tool,
  and queued counts separately.

## Final redesigned remaining experiment matrix (2026-10-18)

This section supersedes every earlier pending matrix. Do not start a new run
until all arm specifications, hashes and execution order are generated,
qualified and frozen.

### Workload and time semantics

Every formal arm uses the complete
`examples/traces/sqlglot-4528-workspace-replay-v2.jsonl` replay with
`time_scale: 1.0`. All 25 recorded LLM durations are preserved one-for-one:
177.50 s total, 6.81 s median and 1.91--14.97 s range per request. The original
recording spans 567.65 s end to end: 177.50 s in LLM calls and 390.15 s between
them in Tool and agent processing. The replay
must sleep for each recorded duration; replacing these waits with 10 ms or
scaling them is experiment-invalid. Tool commands still execute in real VMs.

Idle Checkpoint is therefore a primary dimension rather than a conditional
follow-up. Policy decisions use the independent 2.718 s wait prediction from the
excluded 15five trace. The SQLGlot trace's actual future wait is recorded only
as an oracle for post-run error analysis. For a 4-GiB Tool VM, the current
conservative checkpoint-plus-restore cost estimate is about 0.72 s and the 2x
amortization guard is about 1.44 s, so the recorded waits are long enough for
checkpointing to be logically useful.

The earlier c320/c640 synthetic results remain separate near-zero-think-time
contention evidence. They cannot support the real-trace, Idle Checkpoint or
application-level claims below. The cancelled synthetic
`c320-admission-ablation-01` is not resumed.

### Densities, host resources and time limits

Every comparison is run at both c320 and c640. One Agent owns one 2-vCPU/2-GiB
Runtime VM and one 2-vCPU/4-GiB Tool VM, so the two densities require 640 and
1,280 live VMs respectively when fully resident.

| Field | c320 | c640 |
|---|---:|---:|
| Offered Agents | 320 | 640 |
| Required Runtime + Tool VMs | 640 | 1,280 |
| Compute domain | CPUs 0-159, NUMA 0/1 | CPUs 0-159, NUMA 0/1 |
| LOCAL LOW / HIGH / physical HARD | 128 / 160 / 928 GiB | primary 512 / 640 / 928 GiB; secondary stress Full 384 / 480 / 928 GiB |
| Maximum WARM/shared pool | 384 GiB on NUMA 2 | 384 GiB on NUMA 2 |
| Full-arm live-borrow cap | 192 GiB | 192 GiB |
| Arrival | fixed stagger, 50 ms | fixed stagger, 500 ms |
| Arm / finalization deadline | 2,400 / 600 s | 2,400 / 600 s |
| VM-pair creation concurrency | 8 | 8 |

The c640 preflight must prove that 1,280 VMs plus expected run growth will keep
`/data/cubelet` below its 85% guard. The prior c640 OS arm crossed that guard.
If current capacity cannot provide at least 20% measured headroom, expand the
CubeSandbox storage allocation before c640; do not delete unrelated workloads
or reinterpret a storage-limited result as a policy result. This prerequisite
is independent of the decision to avoid SSD snapshot tiers.

Creation concurrency 8 is the frozen fallback declared before the formal run.
Concurrency 16 failed 13 of the first 25 terminal Agents when provisioning
overlapped trace and checkpoint traffic. Attempt 05 has isolated a smaller
HTTP-408 provisioning burst while the remaining Agents continued; these
ordinary Agent failures remain in the denominator under the stopping rules.

The density-specific LOW/HIGH values are bounded by two preserved c320
pilots. The earlier claim that HIGH=512 GiB completed 112 sessions is
withdrawn: the located raw pilot has no session_complete events. HIGH=96 GiB
produced excessive restore churn and failed Agents. Neither proves a useful
completion regime under the intended actual-idle policy. The c320
128/160-GiB point proved too severe for the 567.65-s application trace: attempt
06 Full reached its 2,400-s deadline with 0 successful Agents after 14,639
completed Tool operations and no OOM. Two early memory-return controller calls
failed because the deployed `numa_borrow.py` expected
`PolicyCoordinator.claim_memory_return`, while the remote `policy.py` was an
older copy without that API. This makes the arm diagnostic evidence rather
than a valid mechanism comparison. It still establishes that this point is an
overload boundary, so Full is not rerun here; the c640 mild point supplies the
next valid Full result.

The c640 plan therefore does not linearly copy the c320 trigger. It first runs
one Full-only point at 512/640 GiB and 500-ms arrival spacing, then reuses that
Full result and runs the four missing P0 baselines at the identical point. This
is the primary comparison because it prioritizes a normally completing result
while retaining enough pressure for admission and checkpoint actions. The
384/480-GiB point becomes a secondary Full-only stress point; it is run only
after the primary comparison if time remains. This order and both run IDs were
frozen before any c640 outcome was viewed. Both retain the same 928-GiB physical
LOCAL hard limit, 384-GiB WARM pool and 192-GiB borrowing cap. These thresholds
change the managed policy trigger, not the installed memory available to
OS-default.

### Canonical Full configuration

`F-S384` is the shared reference at each density. It deliberately enables the
major ClawBox mechanisms together:

| Dimension | Frozen Full setting |
|---|---|
| Deployment | managed |
| Tool admission | predicted, frozen ClawTune artifact, reservation scale 1.0 |
| Fixed-policy calibration | independent artifact P90 resolved by `auto` before qualification |
| Admission constants | non-command 128 MiB; startup headroom 256 MiB; margin 256 MiB |
| Placement | NUMA load-aware over nodes 0/1; template affinity off |
| Idle reclamation | pressure-triggered `wait`, Runtime and Tool VM as one Agent pair |
| Shared borrowing | pressure-based, 192 GiB cap |
| Runtime control | memory protection, release-aware CPU boost and proactive reclaim on |
| Coordination | measured-cost selection between safe idle reclaim and live borrowing |
| Checkpoint | incremental COW, WARM-only, LRU; 1 GiB operation headroom per node; transfer concurrency 4 |
| WARM/shared capacity | 384 GiB on NUMA node 2 |
| Cold/SSD tier | disabled; no COLD spill or tiered storage |
| Sampling | memory 1 s, controller 0.5 s |
| Seed | 20261017 |

The existing independently measured action costs remain frozen. WARM exhaustion
must cause policy waiting or another valid in-memory action; it must never spill
to SSD in this matrix.

### Unique arm matrix

Each row below reuses the same `F-S384` result for the relevant density. Every
ablation changes only the stated field; workload, images, quotas, timeout,
arrival order, prediction artifact and all other Full mechanisms remain fixed.

| Study | Arms at both c320 and c640 | Only difference from Full | Question |
|---|---|---|---|
| A. Admission | `F-S384`, `A-fixed`, `A-off` | Tool admission predicted / calibrated fixed / off | Does admission prevent collapse, and does prediction beat one fixed reservation? |
| I. Idle Checkpoint | `F-S384`, `I-resident` | `idle_reclaim.policy=off` in `I-resident` | Do real LLM waits make in-memory checkpoint/reclaim worthwhile? |
| B. Shared borrowing | `F-S384`, `B-no-borrow` | `shared_memory.borrow=off` | Does live use of the shared NUMA pool improve successful throughput? |
| S. Shared-pool scale | `S-tight`, `S-near-peak`, `F-S384` | Capacity selected from the measured Full peak by the frozen rule below | How much pool capacity is needed before benefit saturates? |
| N. NUMA-aware placement | `F-S384`, `N-round-robin` | placement load-aware / round-robin | Does NUMA-aware placement improve balance, throughput or tails? |
| R. Runtime control | `F-S384`, `R-runtime-off` | all three runtime controls on / off | Does runtime protection and boosting help under actual PSI/contention? |
| D. Deployment reference | `F-S384`, `D-os-default` | complete ClawBox system / OS placement with all ClawBox controls off | Does the complete system beat a true no-prediction/no-admission deployment? |

`A-off` remains a managed arm: it disables Tool admission only while retaining
NUMA-aware placement, Idle Checkpoint, borrowing and runtime control. This is
the causal no-admission comparison. `D-os-default` is a separate end-to-end
system reference and necessarily disables prediction, admission, managed
placement, runtime control, reclamation and borrowing.

`A-fixed` uses the prediction artifact's independent static-memory calibration
resolved by the supported `auto` path and frozen in the qualified YAML. Do not
choose the fixed MiB value from c320/c640 outcomes. `F-S384`, `A-fixed` and
`A-off` therefore compare the admission policy while every other mechanism is
active and identical.

The shared-pool scale study changes both in-memory checkpoint capacity and the
live-borrow cap at a fixed 2:1 pool-to-borrow ratio. First run `F-S384` and
measure the maximum physical WARM residency, `P`, separately at each density.
Before running either capacity arm, freeze `S-near-peak` slightly below `P`
(90% of `P`, rounded down to a 4-GiB boundary) and `S-tight` at half of that
capacity. A capacity greater than the observed peak is effectively unbounded
and is not added as another point. This uses the Full arm only to size the
resource boundary, never to select a favorable outcome. `B-no-borrow` remains
the pure borrowing ablation because it retains the full 384-GiB WARM capacity
for snapshots.

There are ten unique arms per primary density point:

1. `F-S384`
2. `A-fixed`
3. `A-off`
4. `I-resident`
5. `B-no-borrow`
6. `S-near-peak`
7. `S-tight`
8. `N-round-robin`
9. `R-runtime-off`
10. `D-os-default`

The required primary matrix is therefore 20 formal arms across c320 and c640,
plus the c640 mild Full calibration point. The Full
arm is not rerun for every pair. Repeat it only if it is invalid or if pre/post
host baselines show material drift greater than 5% in idle memory, service load
or VM-create throughput.

### Runtime-control detail

The required runtime comparison treats memory protection, release-aware CPU
boost and proactive reclaim as one package. Report their action counts and
service overhead separately. If `F-S384` records nonzero actions for more than
one component and the paper claims those components independently, add the
following secondary arms at both c320 and c640:

- `R-no-memory-protection`
- `R-no-cpu-boost`
- `R-no-proactive-reclaim`

These six optional runs are not substitutes for `R-runtime-off`. A component
with zero actions receives a dormant/neutral conclusion rather than a claimed
benefit.

### Priority and execution order

Run the most consequential evidence first. Within a priority, finish c320 and
verify cleanup before its c640 counterpart. Use the one frozen order below
rather than selecting later arms from observed performance.

| Priority | Arms, each at c320 then c640 | New arms | Reason |
|---|---|---:|---|
| P0a | c320 `A-off`; retain invalid Full only as diagnostic | 1 diagnostic | Closed by the frozen time gate after both Full and no-Tool-admission produced zero successes; three pending arms skipped |
| P0b | c640 primary `F-S384` at 512/640 GiB | 1 | Obtain a lower-contention, normally completing Full point first |
| P0c | c640 primary `A-off`, `A-fixed`, `I-resident`, `D-os-default` at 512/640 GiB | 4 new | Complete the consequential baselines at the same point without rerunning Full |
| P0d | c640 balanced `F-S384`, then `A-off`, `I-resident`, `D-os-default` at 448/512 GiB and 1.0-s arrivals | 1 + 3 conditional | If the 0.5-s point is an unusable CPU/I/O overload boundary, retain c640 scale while reducing phase overlap; screen Full before spending time on the consequential comparisons |
| P1c | c640 balanced `A-fixed` at the same point | 1 conditional | Complete the admission ablation after the consequential comparisons; fixed is secondary because it is a constructed baseline |
| P0e | c640 paced `F-S384`, then `A-off`, `I-resident`, `D-os-default` at 320/352 GiB and 2.0-s arrivals | 1 + 3 conditional | Final real-trace point after 1.0-s arrivals still overloaded Cube at about 300 live Agents; lower offered arrival rate while lowering watermarks to preserve memory-policy activation |
| P1d | c640 paced `A-fixed`, then `B-no-borrow`, `N-round-robin`, `R-runtime-off` | 1 + 3 conditional | Queue secondary attribution only after valid Full and core comparisons at the paced point |
| P1a | c640 stress `F-S384` at 384/480 GiB | 1 | Add a second Pareto point only after the primary comparison |
| P1 | `B-no-borrow`, `S-near-peak`, `S-tight` | 6 | Borrowing attribution and shared-pool capacity curve |
| P2 | `N-round-robin`, `R-runtime-off` | 4 | NUMA-aware and runtime-control attribution |
| P3 | component runtime arms, only under the rule above | up to 6 | Fine-grained runtime attribution if actions exist |

Before the first formal arm, run the normal qualification at concurrency 2 and
one narrow real-VM paired-checkpoint activation check; neither is a performance
point. The check must observe both `runtime` and `tool` roles in successful
pause and restore events, Runtime restore before model-response delivery, and
no stale priority leaf. Do not add a c128 pilot or shorten LLM waits.
One valid run per arm is sufficient. Ordinary Agent failures remain isolated and
in the denominator; only infrastructure failure, contaminated cleanup or
incorrect trace timing invalidates an arm.

### Activation and interpretation requirements

All required ablations run even if a mechanism later proves dormant. Activation
determines what may be claimed, not whether an unfavorable comparator is kept.
The 1:1 model waits reduce simultaneous Tool CPU demand and simultaneous memory
growth, but resident Tool VM pages remain charged until reclamation. Therefore
CPU contention should be lower than in the 10-ms synthetic workload, while
resident-memory pressure can remain substantial. Admission benefit must be
judged from successful throughput and failures rather than assumed from offered
concurrency.

For the Idle Checkpoint claim, `F-S384` should record at least 64 successful
Tool pause/restore cycles and 4 GiB aggregate measured LOCAL release at c320,
and at least 128 cycles and 8 GiB release at c640. These are activation-strength
checks, not filters for discarding an unfavorable result. If they are not met,
retain the `I-resident` comparison and report that the selected watermarks did
not produce enough reclamation activity for a strong mechanism claim.

For every arm, report:

- successful Agents/s, success rate and all-offered P50/P95/P99;
- provisioning, execution and finalization time separately;
- actual active Tool overlap and VM/whole-host CPU equivalents;
- LOCAL, shared-live and WARM snapshot residency, byte-seconds and HIGH crossings;
- admission blocked count/time and predicted versus observed Tool memory;
- checkpoint/restore counts, released LOCAL bytes and service time;
- borrow/return counts, reservation bytes and measured NUMA residency;
- runtime protection, CPU-boost and proactive-reclaim counts and overhead;
- per-node placement counts, imbalance and NUMA memory distribution;
- PSI, reclaim/scan activity, OOM events, isolated Agent failures and cleanup.

A configured switch with zero physical actions is a valid negative result, but
is not evidence that the mechanism improved performance. Configured capacity,
predicted demand, reserved bytes and measured physical residency remain separate
quantities in every table and figure.

## Decision and stopping rules

- Finish c320 P0a, run the c640 P0b Full, then run the four-arm c640 P0c
  continuation at the same 512/640-GiB point. The 384/480-GiB Full stress point
  and mechanism-specific P1/P2 arms follow only if time remains. c1280 remains
  outside this matrix.
- Do not modify code for warnings or issues that do not affect results,
  fairness, cleanup isolation, or reproducibility.
- Fix and rerun only when a defect changes outcomes, disables a selected
  mechanism, propagates one Agent's failure, corrupts metadata, or contaminates
  the next arm.
- One valid run per arm is sufficient. Repeat only an invalid or materially
  anomalous central result.
- Time-priority gate for the c320 continuation: if `A-off` reaches its 2,400-s
  deadline with zero successful Agents, stop the three still-pending c320 arms.
  Together with the diagnostic Full timeout, that establishes the 128/160-GiB
  point as an unusable overload boundary; additional ablations there cannot
  yield a meaningful throughput comparison. Proceed directly to the c640
  512/640-GiB primary point. If `A-off` completes Agents, retain the full c320
  continuation.
- Do not retune within a comparison point. The c640 points above were frozen
  from the c320 boundary before any c640 outcome; every configuration change has
  a new run ID and is not directly paired with a different threshold.
- A queued session is not a running Agent. Claims use successful Agents/s and
  include failures and the complete arm duration.
- Freeze workload, configuration, code revision, seed, timeout, and comparison
  matrix before a final comparison. Preserve negative and excluded runs.

## Required outputs

- Per-arm successful Agents/s, success rate, all-offered P50/P95/P99 latency.
- Actual CPU equivalents, LOCAL/shared/WARM memory, PSI and OOM events.
- Admission/creation/placement queues and mechanism action counts/costs.
- Exact configuration, input hashes, run IDs, execution order and cleanup state.
- A concise result table, figures for the primary comparison, limitations, and
  reproduction commands.

## Historical next actions (superseded)

The supplement execution and stop decision at the top of this file supersede
this queue. The entries below are retained only as an audit trail; they are not
current authorization or a recommendation to resume these arms.

1. c320 qualification, the stale-leaf Linux cgroup test, and the real-VM paired
   Runtime+Tool checkpoint check passed. Formal attempt 01 was invalidated by
   the hidden 30 s `cpuset.mems` timeout; the configured 120 s pool-operation
   timeout now governs that migration. Attempt 02 proved that creation
   concurrency 16 is unstable when provisioning overlaps real trace and
   checkpoint traffic; it was cancelled and cleaned. Attempt 03 uses the
   predeclared fallback of 8. Attempt 03 then isolated the stale-shim borrow
   accounting defect without creation failures; it was cancelled and cleaned.
   Attempt 04 proved that the populated cgroup is Cube's required resume
   tombstone rather than a disposable shim, so that change was reverted.
   Checkpoint now returns borrowed NUMA policy to LOCAL, retains only physically
   backed residual reservation, and lets restore/final destroy release it.
2. Complete the matched c640 512/640-GiB, scale=0.75 `A-off` window after
   Full, then use its actual residency and completion curve to choose the
   next memory budget. Do not expand the entire ablation matrix at a point
   with no successful Agents. Run `I-resident` where Full actually checkpoints;
   the saved 320/352-GiB Full provides activation evidence but does not prove
   a throughput benefit. Keep `D-os-default`, `A-fixed`, borrowing, placement,
   runtime and WARM-size comparisons pending in priority order.
   Do not rerun Full at c320, the skipped c320 arms, the two pressure pilots,
   or any completed valid arm.
3. Measure current CubeSandbox storage use and project the 1,280-VM c640 peak.
   Expand its storage allocation if the projected run cannot retain 20% free
   headroom; do not begin c640 in a known storage-limited state.
4. After each valid Full arm, freeze the two density-specific constrained WARM
   capacities from its measured peak using the rule above; retain all raw event
   logs used for that calculation.
5. Continue with P1 and P2 within the time budget. Add P3 only when recorded
   runtime actions justify component-level claims.

Attempt 05 is invalid diagnostic evidence. Before the unrelated cleanup defect
stopped it, the Full arm reached 420 pauses, 396 restores, 71 shared-memory
borrows and more than 16,000 completed Tool calls with no OOM. WARM residency
reached about 155 GiB of the configured 384 GiB. Ordinary HTTP 408, HTTP 502 and
Tool transport failures initially remained isolated, but one failed Agent's
model-gateway session then failed to drain and cleanup raised
`ArmInfrastructureError`, terminating the arm with 0/320 reported completions.
The run was cancelled and cleanup verified; its raw event log remains for
diagnosis and cannot support a performance comparison.

The narrow repair discards an undrained model-gateway session only when that
Agent has already failed, after both of its VMs close, and records
`model_gateway_session_discarded`. A successful Agent's undrained session still
invalidates the arm. Thirty-five focused tests passed both locally and on
Kunpeng. The replacement qualification `qualification-795bd93c4e9b` completed
all five real-VM arms with verified cleanup. Formal run
`real-trace-p0-c320-pair-l160-06`, attempt
`attempt-af9e5cfa740c4252`, was cancelled and fully cleaned. Its Full arm timed
out after 2,400 s with 0/320 successes, 14,639 completed Tool operations, 120
pause and 118 restore events, 31 borrow actions, six isolated HTTP 408 Agent
failures, and no OOM. Two controller failures caused by the stale remote
`policy.py` invalidate mechanism attribution from this arm. The next `A-off`
arm was stopped early and is not used. The matching policy source was deployed
and its 47 policy/NUMA tests passed. Qualification
`qualification-9b7f1cf9a792` then passed all four real-VM arms, paired
incremental-COW checkpoint/restore, two-node borrow/return/reborrow, and
verified cleanup. Formal continuation
`real-trace-p0-c320-remaining-l160-01`, attempt
`attempt-577b6cbfdfaf4b21`, ran `A-off` to the 2,400-s execution boundary. It
recorded 0 successful Agents, 19 isolated Agent failures, 31,154 completed Tool
operations, 236 pauses, 225 restores, 72 borrows, five returns, no OOM, and no
controller or policy-event failures. WARM usage reached at least 112 GiB in a
live observation. The time-priority gate then skipped `A-fixed`, `I-resident`,
and `D-os-default`. Abort initially encountered eight run-owned Cube shims in
the saved priority domain; after terminating only those verified PIDs, the
public cleanup succeeded, removed all 448 run-owned cgroups, and marked cleanup
verified. This cancelled raw run is diagnostic overload evidence rather than a
formal throughput result. The guard then began c640 Full qualification.

The guarded continuation is now complete: c640 begins only after the c320 time
gate and verified cleanup. The first c640 launch inherited a 1,024 soft
file-descriptor limit because it bypassed the supported wrapper and failed with
`EMFILE` while creating Agents; it was aborted and cleaned and is excluded. The
second launch correctly used the wrapper but found one stale qualification VM in
the physical VM domain, failed before its arm began, and cleaned up; it is also
excluded. The stale VM was identified from its qualification ownership journal
and removed with the supported cleanup command. Run
`real-trace-p0-c640-mild-l640-03` is the first correctly launched formal
attempt: both its supervisor and worker had a 65,536 soft and 524,288 hard FD
limit. It ended at the 3,000-s supervisor boundary with a recorded timed-out
trial, 0/640 completed Agents, cleanup verified, 575,349 aggregate seconds of
Tool-admission waiting, four pauses, and no memory-watermark crossing. A live
observation after about 27 minutes recorded 183 isolated HTTP 408 Agent
failures, 777 sandbox creations, 26,413 completed Tool calls, eight
shared-memory borrows, no controller/policy failures, about 490 GiB in the Cube
parent cgroup, zero memory PSI, load average about 350, and substantial I/O PSI.
One session directly recorded 901.7 s in the VM-create queue. Thus the 0.5-s
arrival point is a Cube lifecycle, I/O and command-latency overload boundary,
not a useful memory-policy comparison. Its four-arm matrix was skipped.

A second guarded fallback is predeclared rather than tuned after seeing its
performance. If and only if the 0.5-s Full has below 80% Agent success, c640
balanced uses 1.0-s fixed arrivals, 448/512-GiB LOCAL watermarks, a 512-GiB pool
budget, a 192-GiB borrow cap, the same 384-GiB WARM capacity, and a 1,200-s Tool
command timeout. It first runs Full. Only a valid Full with completed Agents and
material admission/checkpoint pressure unlocks `A-off`, `I-resident`, and
`D-os-default`. `A-fixed` is a separate P1 arm after those comparisons. This
keeps the c640 request scale and 1:1 LLM waits while moving away from the
observed CPU/I/O cliff and preserving enough memory pressure to exercise the
mechanisms. This 1.0-s screen was also stopped after about 18 minutes: it had
0 completions, 54 independent failures, 615 created VMs, 18,540 completed Tool
calls, six borrows, two returns, no pauses and about 383 GiB current VM-cgroup
memory. HTTP 408, proxy 502 and envd-reset errors showed that Cube control-plane
load still dominated before the 512-GiB memory watermark could activate. The
cancelled diagnostic was cleaned after terminating only 26 root shims whose
`/proc/<pid>/cgroup` paths exactly matched the run-owned prefix; the public
abort then verified cleanup and the physical VM domain returned to
`populated 0`.

The first paced run `real-trace-p0-c640-paced-full-l352-01` is excluded. Its
first attempt ran cleanly until the host unexpectedly rebooted; the resumed
attempt reached 428 sandbox creations, 7,689 completed Tool operations, six
pause/restore cycles and two borrows with zero completions and five isolated
Agent failures. Cube then returned `no more resource`. The decisive host fact
was Cubelet `tap_init_num = 500`, although c640 may need 1,280 simultaneous
Runtime and Tool VM interfaces while Agents remain live. The attempt was
cancelled and cleanup verified. This is infrastructure-limit evidence, not a
policy result.

The TAP pool was raised to 1,536. Cubelet pre-created the complete pool (1,563
total host network interfaces including non-pool devices), snapshot storage
registered, and a real VM create/execute/destroy doctor probe passed. The clean
replacement `real-trace-p0-c640-paced-full-l352-02`, attempt
`attempt-63b01b3701324682`, is now running. It retains 640 offered Agents and
1:1 model waits, uses a 2.0-s fixed arrival
interval, 320/352-GiB LOCAL watermarks, a 352-GiB admission budget, a 192-GiB
borrow cap, the 384-GiB WARM pool and a 1,200-s Tool timeout. This halves VM
arrival pressure relative to the failed 1.0-s point while lowering the memory
trigger to the usage range inferred from that point. Full must complete at least
512 Agents and materially activate admission or checkpointing before the guard
launches `A-off`, `I-resident` and `D-os-default`. A separate validated guard
then queues `A-fixed`, followed by borrowing, NUMA-placement and runtime-control
ablations. Constrained WARM sizes are generated only from the valid Full's
measured physical and enforced-charge peaks.

Before this launch, 2,667 S3lvol rotated log files older than two hours were
removed from `/data/log/rcow`; current service logs and all per-run result
artifacts were preserved. This released about 66 GiB. Cube garbage collection
then brought root-filesystem free space to about 560 GiB. The c640 continuation
guard is protected by `flock` and will launch core comparisons only after the
Full result envelope and cleanup verification are both present and the Full
passes the frozen informativeness rule.

The clean 320/352-GiB replacement was stopped by the same frozen screen rule
after about 19 minutes. It had 0 completed Agents, 16 isolated session failures,
458 created sandboxes, 50 successful pauses (25 Runtime and 25 Tool), 43
restores, 10 borrows, about 29 GiB WARM use and no OOM. Creation remained at
458 sandboxes for more than seven minutes while individual Tool admissions
waited about 35 seconds. At most roughly 229 session pairs had entered the
workload, so the arm could not reach the required 512 completions in its
remaining window. It is retained as direct evidence that 320/352 GiB is an
over-conservative admission regime for the 567.65-second application trace.
This remains a useful point on the resource/successful-throughput Pareto
frontier: report its lower live-memory footprint together with its zero
completed-Agent throughput, queueing, tail behavior and mechanism counts. Do
not rerun it and do not delete its raw event, lifecycle or cleanup artifacts.
Eight residual Cube shim PIDs were enumerated only under the exact run-owned
cgroup prefix, verified through `/proc/<pid>/cgroup`, terminated, and recorded;
the public abort then reported cleanup success and the parent VM cgroup returned
to `populated 0`.

The next predeclared Pareto point changes only the memory budget: total
LOW/HIGH/pool budget are 512/640/640 GiB (256/320 GiB per compute NUMA node).
It retains c640, the 2.0-second fixed arrival schedule, 1:1 recorded waits,
384-GiB WARM, 192-GiB borrow cap, images, prediction data, seed, create
concurrency 8 and timeouts. Full qualification `qualification-eff4435df4c4`
passed real VM, paired checkpoint/restore, two-node borrow/return/reborrow,
supervision-fault and cleanup checks. Formal run
`real-trace-p0-c640-relaxed-full-l640-01`, attempt
`attempt-2683c13570654a7f`, is excluded as infrastructure-failure evidence. It
created 554 VMs and completed 12,056 Tool calls with no Agent outcome before VM
creation stopped. S3lvol then repeatedly reported missing S3 chunks and failed
dirty-chunk flushes. The run was cancelled and cleanup verified; its events
cannot be used for a policy comparison.

The unusable S3lvol store was backed up and reset with the maintained purge
workflow while the host was idle. A reboot cleared a separate stale BPF/TAP RCU
stall. The c640 TAP pool is now the exact worst-case 1,280 Runtime plus Tool
VMs, Cubelet reports healthy, the repaired store checkpoints normally, and
host apply passed a real VM creation/execution/destruction probe. Fresh Runtime
and Tool templates retain the same immutable image digests, vCPU, memory and
kernel identities. The frozen prediction artifact retains its predictions and
records only the new Tool template ID.

Replacement qualification `qualification-95f0aaef71a7` passed at smoke
concurrency 2, including paired Runtime+Tool incremental-COW
checkpoint/restore, two-node borrow/return/reborrow, placement, supervision
fault handling and verified cleanup. Formal run
`real-trace-p0-c640-relaxed-full-l640-02`, attempt
`attempt-b91fe025ae1247cc`, ended after 2,843.8 seconds with cleanup verified
but an invalid infrastructure `failed` envelope: one ModelGateway session did
not drain during post-deadline convergence. It is not rerun. Its raw evidence
is retained as a Pareto-boundary diagnostic: 0/640 successful Agents, 15,632
completed Tool calls before convergence, two independent Agent failures, no
OOM or policy/controller failure, no checkpoint/restore/borrow action, and
about 485 GiB steady Cube-cgroup memory. Across 416 sessions that had emitted
timing records during convergence, request-to-cleanup P50/P95/P99 were
2,094.6/2,453.2/2,455.9 seconds. Those are censored request-to-cleanup
times, not successful completion latency. The previously labelled 1,817.7/
1,866.5-second Tool waits mixed creation/placement waits and are withdrawn.
Within the first 1,200 seconds, the exact completed-admission
`admission_blocked_seconds` P95/P99 were 2.47/318.38 seconds. Logs and lifecycle artifacts must not be deleted, but
the point is too conservative for a full ablation matrix.

The next primary point keeps c640, 2.0-second arrivals, 1:1 recorded LLM waits,
the 384-GiB WARM pool and 192-GiB borrow limit, while raising total LOCAL
LOW/HIGH and the admission budget to 768/896/896 GiB (384/448 GiB per compute
NUMA node). This leaves 32 GiB below the measured 928-GiB LOCAL capacity and
allows 40% more logical reservation than the 640-GiB boundary. It uses a
1,500-second measurement window plus 300 seconds for finalization. Qualified
run `real-trace-p0-c640-main-full-l896-01`, attempt
`attempt-0a78c934f47c4fd4`, is running Full first. Around the trace's
567.65-second natural duration, the early Agent completion curve determines
whether this is the useful main point; do not wait for the hard limit merely to
declare another overload boundary. Only after Full demonstrates useful
successful throughput run matched `A-off` and `D-os-default`. `I-resident`
remains unnecessary unless Full actually checkpoints. Fixed and secondary
mechanism ablations remain conditional and lower priority.

The 896-GiB Full was stopped after about 10.6 minutes during the arrival
ramp. This retained partial point does not establish low contention after
all requests arrive: it had 569 created
VMs, 12,453 completed Tool calls, 0 Agent completions or failures, about
412.7 GiB current Cube-cgroup memory, and no checkpoint, restore, borrow,
controller failure or policy failure. Its first public abort timed out with 84
run-owned shims. Their exact priority-domain cgroups and PIDs were recorded in
`bounded-cgroup-kill.json`; after bounded `cgroup.kill`, the public abort
succeeded and the parent cgroup reported `populated 0`. Raw logs are retained.

The one-hour primary evidence gate now runs only matched c640 Full and `A-off`
at the 512/640-GiB point with prediction reservation scale 0.75. The scale is
an exploratory reduction motivated by the gap between the old 640-GiB logical
admission boundary and about 485 GiB physical residency. That aggregate ratio
does not calibrate per-tool predictions or imply a linear increase in admitted
work: baseline residency, pending growth and creation headroom also count.
Each arm has a 1,200-second observation
window and 180-second finalization limit. Qualified Full run
`real-trace-p0-c640-balanced-full-s075-01`, attempt
`attempt-1421d77e952d413e`, is running. Inspect the first completion curve and
mechanism activation around the trace's 567.65-second natural duration; stop
early and retain the point if it is plainly over- or under-loaded. Run the
matched no-admission arm next. OS default, fixed and mechanism attribution wait
until this primary pair is recorded.

Reporting corrections for the short-window comparison: at 2-second fixed
arrivals the last of 640 requests arrives at 1,278 seconds, outside the
1,200-second window. Report target count and actually offered count separately;
this is a finite-window screen, not a completed c640 batch. Successful
Agents/second uses the actual observation duration. Unfinished requests are
censored, not assigned invented completion latencies. Completed-only latency
quantiles must be labelled and cannot stand in for all-request P95/P99.
`tool_completed` includes backend operations; separate execution scopes before
calling it application-tool throughput. The raw replay has 25 recorded LLM
spans totaling 177.503573177 seconds of model wait; the previously cited
567.65 seconds refers to the original application timeline, not a guaranteed
replay completion time. Keep every partial/failed-run event file and mark
post-stop cleanup observations separately from workload-window observations.

At 2026-10-18 13:48 CST, the scale=0.75 Full ended as `timed_out` with
cleanup verified. Its 1,200-second window contained 600 scheduled arrivals,
0 successful Agents, 33 independent failures, 4,681 completed agent-tool
executions and 12,924 backend-maintenance executions. LOCAL sampled peak was
510.07 GiB, with two borrows, one return and no checkpoint. Admission wait
P95/P99 among admitted operations was 7.21/39.91 seconds, excluding pending
requests. The final envelope's 640 failed-to-complete targets must not be
misreported as 640 independent exceptions. Matched `A-off` passed qualification and started at 13:49:30 CST after
verified cleanup. Its unchanged 1,200-second window is running; the
all-mechanisms-off OS baseline is queued behind verified cleanup. Detailed raw-window summaries
and the comparison table are in `REAL-TRACE-WINDOW-RESULTS.md`.

## Locations

- Local configuration pack: `.experiment-audit/c1280-design-20261017/`
- Remote study root: `/home/weitianc/clawbox-c1280-20261017`
- Remote results: `/home/weitianc/clawbox-c1280-20261017/results`
- Applied host profile: `/home/weitianc/clawbox-c1280-20261017/configs/host.json`
- Observed foreground idle gate and c320 launcher:
  `/home/weitianc/clawbox-c1280-20261017/run-c320-when-idle.sh`

## Additional Tasks

When you finish everything above, if you still have time, please finish additional experiments in ClawBox/LLC.md
