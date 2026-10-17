# ClawBox c1280 evaluation tracking plan

Last updated: 2026-10-17 (Asia/Shanghai)

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
| P0.3 | c1280, OS default vs predicted | BLOCKED ON STORAGE HEADROOM | Do not start while Cube storage can cross its 85% guard before intended memory contention |
| P0.4/P1/P2 | real trace and activated mechanisms | PENDING P0 | Run only the selected point/activated mechanisms |

## Fixed resource configuration

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

The applied `/sys/fs/cgroup/cube_sandbox/sandbox` parent was verified with
effective CPUs `0-159` and memory nodes `0-2`. Thus the OS-default arm inherits
the same 160-CPU ceiling while Linux manages the combined memory domain; it
cannot consume the controller CPUs `160-319`.

## Priority order

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

#### P0.3 Controlled c1280 overload boundary

- Config: `.experiment-audit/c1280-design-20261017/c1280.yaml`
- Status: PENDING c640 contention/progression review.
- Arms: `predicted-shared` and `os-default-no-admission` only.
- Purpose: determine sustainable throughput and failure behavior at the maximum
  offered concurrency, not claim 1280 simultaneous execution.
- Start gate: c640 reaches real resource contention and continues semantic
  progress; cleanup is verified. Prefer at least 120 busy VM CPU equivalents
  and either 700 GiB LOCAL use or sustained memory PSI.
- Required reporting: offered, VM-started, resident, checkpointed, active Tool,
  and queued counts separately.

#### P0.4 Real-trace F/B confirmation

- Status: PENDING controlled boundary selection.
- Workload: one frozen selected SWE-ReBench trace with its original Tool order
  and model waits.
- Arms:
  - F: complete system with formal frozen KB prediction and admission.
  - B: OS-default/no-admission system reference under the same resource limit.
- Density: one contention point selected before viewing final F/B outcomes;
  likely c160 or c320 rather than a broad sweep.
- Purpose: application-level predictive-admission evidence. If KB granularity
  remains too coarse, retain the negative result and restrict the positive claim
  to the controlled exact-command mechanism study.

### P1. One combined-system comparison and two central ablations

Run these at one contention point selected from P0. Do not run them at every
density.

#### P1.1 Full versus practical resident reference R

- F: all selected ClawBox mechanisms.
- R: calibrated fixed admission, resident VMs, no runtime boost/protection, no
  idle checkpointing, no dynamic borrowing, and balanced placement.
- Give R the same workload, CPU budget, validation, timeout, and equal total
  memory where topology permits.
- Claim: combined system benefit relative to this reference; no attribution to
  one mechanism.

#### P1.2 Shared live-memory borrowing ablation A4

- F: `shared_memory.borrow=pressure`.
- A4: change only borrowing to `off`; retain WARM checkpoints and all quotas.
- Run only if P0 records real borrow actions or LOCAL pressure that borrowing
  could relieve.
- Claim: contribution of live borrowing under the chosen partition.

#### P1.3 Idle reclamation/checkpoint ablation A3

- F: wait-aware idle Tool reclamation enabled.
- A3: change only idle reclamation to resident.
- Use a trace with multi-second model waits; the current controlled trace has
  only 80 ms total model wait and is unsuitable.
- Run only if the pilot records checkpoint/restore actions.
- Claim: contribution and cost of reclaiming idle VMs.

### P2. Conditional mechanism evidence

Run only for mechanisms retained as paper contributions and only after pilots
show activation.

- Runtime memory: F versus F minus `memory.low` protection.
- Runtime CPU: F versus F minus release-aware CPU boosting.
- NUMA placement: load-aware versus balanced round-robin, with template
  affinity disabled in both arms and identical eligible CPUs/memory.
- Coordination: measured-cost selection versus reclaim-first, only when both
  borrowing and reclamation are viable concurrent choices.

Do not claim an independent benefit for a skipped or non-activating mechanism.

## Decision and stopping rules

- Prioritize P0. Do not delay c640/c1280 admission evidence for secondary
  ablations or cosmetic infrastructure work.
- Do not modify code for warnings or issues that do not affect results,
  fairness, cleanup isolation, or reproducibility.
- Fix and rerun only when a defect changes outcomes, disables a selected
  mechanism, propagates one Agent's failure, corrupts metadata, or contaminates
  the next arm.
- One valid run per arm is sufficient. Repeat only an invalid or materially
  anomalous central result.
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

## Current next actions

1. Preserve the completed c320 boundary: predicted completed 320/320 while
   OS/no-admission completed 0/320 before timeout. Treat it as a catastrophic
   contention boundary rather than quoting an undefined finite speedup.
2. Preserve the completed c640 boundary: predicted completed 640/640 at
   21.28 Agents/min while OS/no-admission completed 0/640 inside the fixed
   window. Report the OS storage-capacity confound and the three post-deadline
   completions explicitly.
3. Do not start c1280 until the saved host configuration proves enough Cube
   storage headroom to keep the platform writable through the intended VM
   population. The maintained crash-dump cleanup preview found zero removable
   bytes; do not delete experiment results, VM/template storage or unrelated
   workloads to manufacture headroom.
4. After the admission boundary is established, freeze one real-trace F/B point,
   then run only the P1 comparisons whose mechanisms activated.

## Locations

- Local configuration pack: `.experiment-audit/c1280-design-20261017/`
- Remote study root: `/home/weitianc/clawbox-c1280-20261017`
- Remote results: `/home/weitianc/clawbox-c1280-20261017/results`
- Applied host profile: `/home/weitianc/clawbox-c1280-20261017/configs/host.json`
- Observed foreground idle gate and c320 launcher:
  `/home/weitianc/clawbox-c1280-20261017/run-c320-when-idle.sh`
