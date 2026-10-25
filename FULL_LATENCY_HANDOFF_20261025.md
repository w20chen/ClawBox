# ClawBox Full Latency Optimization: Execution Plan for an Independent Session

Updated: 2026-10-25 (Asia/Shanghai). This document is an execution handoff, not a report of completed optimization work.

## 1. Objective and decisions confirmed by the user

The next session has at most five hours to complete the necessary environment updates, diagnose and fix excess latency in Full, run formal experiments, and deliver the results. The clock starts when that session begins the work. Environment preparation, testing, cleanup, and reporting all count toward the limit. The session that prepared this plan did not start the five-hour experiment task.

The primary question is where time outside tool execution is spent: VM creation, admission queues, the control plane, synchronization, and result delivery. Identify unnecessary waits and measure how much the fixes reduce them. End-to-end p90 is an outcome metric; also report the absolute seconds and percentage attributable to overhead. Do not optimize for the number of code changes.

The user confirmed:

- NUMA nodes 0 and 1 each provide 500 GiB of LOCAL memory and 80 logical CPUs, for a total of 1,000 GiB / 160 CPUs. NUMA node 2 provides 100 GiB for shared/WARM memory. The experiment does not use all resources on the physical host.
- Replay the traces for the LLM stage, preserving the recorded LLM wait times, while executing the tools for real. Do not replace replay with calls to a paid model service.
- 1280 and 2560 are the total numbers of sessions offered in each run, not simultaneous active-session counts that must be maintained throughout. Assign the 503 tasks round-robin, with an independent writable environment for every session.
- Use Poisson arrivals with a common expected arrival window for both scales. Freeze the window, random seed, and generated arrival sequences. The arrival rate for 2560 is therefore approximately twice that for 1280.
- Do not run 503 single-concurrency baselines, separate c64/c128/c256 performance ramp-up experiments, or extensive preliminary experiments. Prefer existing pilot records when selecting the arrival window. Retain only necessary qualification checks and correctness tests targeted at the changes, then proceed to formal Full-1280 as soon as possible.
- The latest clarifications take precedence over single-session runs, lengthy c256 runs, OS-default runs, or the barrier-based acceptance task requiring 1280 simultaneously resident sessions in older handoffs. Do not revive those tasks.
- Full must retain all agreed mechanisms. Do not manufacture a performance improvement by disabling admission, sharing, reclamation, or prediction, or by increasing resource budgets. Any necessary diagnostic comparison that changes one mechanism must be labeled separately and must not be reported as a Full result.
- Use ClawTune bootstrap-v2 and retain permissive ingestion of workload measurements. Short sampling periods and incomplete attribution are not automatic reasons for rejection. Respect the values and provenance returned by ClawTune; do not independently replace arbitrary missing values with zeros.
- The minimum reservation derived from tool predictions has already been changed to 0 MiB. Preserve P50 and the existing scaling logic. A zero reservation may bypass the tool memory-budget wait. Do not restore the 1 MiB floor, and do not misreport a zero reservation as an unexecuted tool.

No numerical speedup target was specified, and completion of both full replays within the available time is not guaranteed. Do not invent acceptance thresholds such as “p90 must be less than twice the single-session baseline.” The objective is to eliminate as much demonstrably unnecessary waiting as possible under fixed experimental conditions, and to report complete or truncated evidence accurately.

## 2. Starting points and state that must be preserved

Local repository: `C:\Users\29068\Desktop\ClawBox`; ClawTune: `C:\Users\29068\Desktop\ClawTune`; remote access: `ssh kunpeng`, with user home `/home/weitianc`.

The evidence directory accompanying this plan is `.runtime/full-latency-handoff-20261025/`. Its `inputs/` directory contains copies of the historical handoff, the historical full configuration, and the latest run-state inspected for this handoff. See `source-state.json` for source identity and `working-tree.patch` plus `untracked-source/` for working-tree changes. These are handoff snapshots: compare them with the workspace at execution time before applying anything, and do not overwrite newer work.

ClawBox HEAD at handoff: `3593eb8e3cf82e357dc535ecfa82210a3aef076c`, with substantial uncommitted ClawTune adaptation changes. ClawTune HEAD at handoff: `a5b976ba3568b826a6a1cf98a279efe2d81fefa2`. Preserve both the tracked diff and `tests/test_clawtune_adaptation.py`. Do not run git reset/clean or overwrite the current code with an older remote source snapshot.

Read the project's AGENTS.md, `docs/guide.md`, and `docs/installation.md` first. The only supported formal execution path is `clawbox experiment`; do not create a second experiment runner. Offline analyzers, low-overhead monitoring, and scripts for batch environment updates are allowed.

Use new experiment/run/attempt identities after changing code, configuration, images, or the seed. Preserve the frozen YAML, source snapshot, input hashes, qualification receipt, run-state, events, owned-sandbox ledger, and host/launch logs. Do not overwrite failed or cancelled records or relabel them as successful.

Do not delete the existing 503 images/templates, experiment results, VM storage, or unrelated workloads. Do not prune Docker volumes to free space. Check the actual targets of symbolic links and bind mounts. Earlier disk expansion and block deduplication work are outside this task.

## 3. Known environment status: what can be reused and what remains

Environment audit: `.runtime/kunpeng-env-audit-20261025/audit.json`. The following were checked on 2026-10-25:

- All 503 `/testbed/.git` directories, 503 traces, and 503 linux/arm64 Docker images exist.
- All 503 Cube templates are READY and match their recorded image digests.
- All 503 still use collector revision `1bdb4c64ea4e9f71231cdd749eb085fbc617f67e`.
- Historical VM smoke-test records exist for all 503 environments. This does not establish successful full replay of all 503 traces. Full-length replays were explicitly skipped during the earlier environment preparation; do not add them as a prerequisite now.

Authoritative environment locations:

```
/home/weitianc/clawbox-swe-images-20261021/
  workspaces/<case_id.lower()>/testbed/
  build/final-state.json
  build/collector-state.json
  build/cube-collector-state.json

/mnt/clawbox-data/artifacts/swe-readiness-20261021/environment-catalog-20261022/latest.json
```

Read the frozen manifest/workload referenced by latest.json. At this handoff it points to `snapshot-20261022T104346.199284Z/workload-ready.json`, SHA256 `6c1b6d1f33a9dd33ce3e51d46bd95138f253aee1b95f4e53fde6c296e60477ff`. Do not use the stale readiness.json at the preparation root, the empty cube-ready-workload.json, or `/data/swe-rebench-arm64-map.json`, which contains only one task.

The existing environments preserve the official task source, Git state, and Python/Conda/project dependencies. Do not clone/reset the repositories or rebuild dependencies for all 503 tasks. The task Python is usually `/opt/conda/envs/testbed/bin/python`; the collector uses a separate Python environment. The image's `/testbed` directory and the runtime workspace configuration are distinct. Resolve them using each case's configuration and preflight check; do not change them all to `/workspace`.

The necessary update is to the ClawTune collector/contract/runtime code layer:

1. Sampling aggregation and the semantics for retaining measurements when errors occur have changed. Updating only the host prediction package while leaving all 503 guests reporting the old labels is insufficient.
2. The embedded BPF C in `tool_resource/telemetry.py` also contains changes to exec timing/mapping. BCC compiles and loads it when the collector starts; rebuilding or replacing the Linux kernel is unnecessary.
3. Reuse the existing matching BCC/LLVM, kernel headers, ToolBridge, envd, and task dependencies. There is no evidence requiring wholesale apt/pip upgrades.
4. Freeze one new collector package, apply it as an overlay to the existing task images, and register new immutable templates. Save the old/new case/image/template mappings separately. Do not alter the provenance of old templates.
5. Do not run the generic rebuild path 503 times if it flattens images, copies kernel headers, and upgrades pip each time. Prefer extending the existing build script with a collector-only update path that reuses the existing content, resumes interrupted work, and bounds concurrency. First check whether the current script already supports this to avoid duplicate implementation.

Already validated adaptation work is recorded in `.runtime/clawtune-adaptation-20261025/validation.json`: 144 targeted Linux tests, 3 trace round-trip tests, installation and repeat execution in an isolated fresh Python venv, and successful execution, telemetry, pause/restore/cancel/cleanup on one real Runtime/Tool VM pair. This does not establish clean-host installation or acceptance of 503 updated templates.

Validated new Runtime template: `tpl-85b06822df05466eb68fd4df`, digest `sha256:f86d86f95f538cbf579141eb38a4d7051dee8640aac5f13beac0f10b7a7cc336`. Prefer reusing it once its identity and content are verified; do not rebuild it merely to reproduce the same default bootstrap-v2 behavior.

The new example Tool template `tpl-bafe9185de4b4424a1bc6de4` validates the integration path only. It is not a universal replacement for the 503 task environments.

The remote adaptation directory `/tmp/clawbox-adapt-20261025/{ClawBox,ClawTune,host.json,machine.env,fresh-venv,results}` may have been cleaned up; check that it exists. Do not assume its source includes the final 0 MiB change. Verify it against the handoff source hashes.

## 4. Host recovery and capacity: the actual blocker before formal launch

The audit found `memory.max=max` in the VM cgroup and a missing `cpuset.mems.effective`; the configured memory/NUMA/swap checks failed. Formal launch therefore had not passed its checks. Running Cube services alone do not establish readiness.

Verified CPU topology: NUMA 0 = CPUs 0-79, NUMA 1 = 80-159, NUMA 2 = 160-239, NUMA 3 = 240-319. The physical host has 320 logical CPUs; that must not become the experimental CPU budget. Restrict compute VMs to CPUs 0-159. Use NUMA 2 for the agreed shared/WARM memory, and do not use NUMA 3 to expand experimental resources. Record control-plane thread/process affinity and actual CPU use, keep them consistent across comparisons, and do not treat undocumented extra CPUs as an optimization.

A suitable historical host plan is:
`/data/clawbox-performance-20261022T154600Z/recovery/host-shared100-20261022T212900Z.yaml`. The adjacent JSON is the result of applying it at that time, not proof that the current host still matches.

Execution order:

1. Inspect current experiment processes, the owned VM inventory, cgroup population, existing background preparation services, and package caches. Old PIDs are clues only; verify process identity before acting.
2. Back up the current host/machine configuration, produce a host plan with this task's exact budget, and inspect and save the diff. Use the existing CLI host check/apply commands to restore the actual cgroup controllers, NUMA, swap, quotas, and mounts. If this would stop active work unrelated to this task, consult the user first; do not automatically clear the host.
3. Specifically check VM/TAP/FD/API limits: 1280 pairs require up to 2560 VMs, while 2560 pairs require up to 5120 VMs. The historical `cubelet_tap_init_num: 2560` covers only the former and does not establish support for 2560 sessions. Check and prepare service limits for the 5120-VM upper bound relevant to this task. Configured capacity is distinct from measured active counts and actual memory use.
4. LOCAL = 512000 MiB × 2, totaling 1024000 MiB. The total shared/WARM budget is 102400 MiB. Live borrowing and WARM snapshots share that budget; do not allocate 100 GiB independently to each and then use both allocations at once.
5. Preserve the existing local low/high watermarks and their meaning in the frozen configuration. The historical LOCAL capacity was 1000 GiB, while the admission budget/high watermark was 960 GiB. Do not conflate these values or raise the latter to 1000 GiB to conceal waiting.
6. Historical package-cache endpoints are APT `http://193.124.7.2:3142` and PyPI `http://193.124.7.2:3141/root/pypi/+simple/`. Probe once through a fresh VM's actual network path. Network permissions in the catalog may differ from those in the formal workload; preserve the exact permissions and allow-list required by the tasks. Do not change project dependency versions to work around network problems.

Do not reinstall the shared host when checks establish that existing components can be reused. If adding public options or maintenance scripts, update the guide and option reference, and verify fresh-shell use, repeat execution, failure exits, and cleanup. Do not repeat installation acceptance for unrelated existing features.

## 5. Historical performance evidence and current code: where to resume

Primary historical study: `/data/clawbox-performance-20261022T154600Z`.

First read the local copy of `inputs/HANDOFF-20261023.md`. It describes the morning of October 23. A later afternoon configuration/run also exists and was inspected while preparing this handoff. Do not infer that no later run occurred from the morning handoff alone:

```
full/full-c1280-clawtune-v3-v5-20261023T134500CST/experiment.yaml
runs/full-c1280-clawtune-v3-v5-20261023t134500cst/run-state.json
```

The afternoon run did start and was subsequently cancelled by the user. Its run-state is cancelled and terminal, with arm cleanup_verified=true. The nested arm.status=running is stale text and does not establish that the process is still alive. Its event directory is under the recorded artifact_root and can support diagnosis, but it is not a complete successful baseline.

That afternoon YAML references the old `bootstrap-v3-coverage`, which must be replaced with the user's current choice, bootstrap-v2. Do not copy the old seed, template identities, or qualification/sharing proof unchanged. Use that YAML as a reference for Full mechanisms and cache configuration, not as a ready-to-run specification.

Analysis of the earlier unhealthy Full-1280 run:
`launch/full-c1280-public-v3-20261022T204100Z/baseline-latency-analysis.json`.

That run was cancelled before completion. Observations included tool queued-to-admitted p90 of 104.28s, memory_prepare p90 of 101.27s, and actual tool execution p90 of 0.506s; mean CPU use was 66.9/160, maximum shared-live use was 15.94/100 GiB, and maximum LOCAL use was 946.8 GiB. This supports investigating the control/admission path, but it is not a complete post-fix p90 result. Subtracting two stage p90 values does not yield per-request waiting times.

The optimization work is not starting from scratch. Current code already includes parallel Runtime/Tool creation, exact sampling outside the lock, feasible-prefix admission, aggregate reservations for multiple tools in one VM, nonblocking sharing, and an activity tracker for both queues. At handoff, the hashes of `admission.py` and `cube/lifecycle.py` match the morning v5 snapshot. Other files have subsequent changes; verify their semantics individually rather than reverting them wholesale.

Key files and entry points:

- `clawbox/experiments/worker.py`: `start_sandbox_pair`, `session_offer_offsets`, `ArmActivityTracker`, `aggregate_admission_queue_snapshots`, `EventWriter`, stage events, and result aggregation.
- `clawbox/experiments/policy.py`: `PolicyCoordinator.acquire`, `_feasible_progress_prefix_locked`, `queue_snapshot`, reservation release, and victim/borrow coordination.
- `clawbox/experiments/admission.py`: demand claims, coalesced sampling, and reconciliation of sampled results with concurrent changes.
- `clawbox/cube/lifecycle.py`: creation, cgroup/NUMA handling, checkpoint/restore, and lifecycle stages.
- `clawbox/experiments/openclaw_driver.py`: the Runtime/Tool bridge, LLM replay, tool result delivery, and dependency installation.
- `clawbox/experiments/spec.py`, `design.py`, `configure.py`, `qualification.py`: public configuration, compilation of Full mechanism settings, and qualification binding.

The earlier single-task SQLGlot study is at `/home/weitianc/clawbox-sqlglot-throughput-20261020`. Its followup directory contains before/after GIL samples, control-stage data, the all-run report, and Full-2560 data. These may identify likely hotspots, but the workload differs and cannot serve as a direct performance comparison for this study's 503 heterogeneous traces. The old CURRENT_PLAN also includes obsolete four-NUMA/1800 GiB settings and a barrier task; do not inherit those budgets or tasks.

## 6. The shortest complete environment-preparation path

Priority order: reuse all task base environments → reuse the new Runtime → build one collector update layer → produce a new case/template catalog → perform necessary qualification → run Full-1280.

Requirements for batch environment updates:

- Use exact old image IDs/digests as inputs. Record case_id, old/new images, old/new templates, collector revision, package file hashes, architecture, and testbed identity.
- Compare task source/dependency identities before and after the image update. Use the existing manifests for incremental verification rather than rebuilding and running lengthy tests for all 503 tasks.
- Bound concurrency and enforce disk-space thresholds. Allow failed entries to be retried and completed entries to be skipped. Record stage durations, distinguishing OCI updates from Cube ext4 materialization.
- Newly registered templates may still require full rootfs materialization. Updating only the collector does not mean that 503 templates update instantly. This is a real risk to the five-hour budget. Measure throughput on an early item and estimate completion time; do not promise that the whole batch will finish in a few minutes.
- Do not introduce an ad hoc second execution path that injects the collector into each VM to avoid updating templates, unless the existing supported path already provides a clearly verifiable equivalent capability. If proposing an integration architecture change, explain the tradeoffs and consult the user first.
- On one representative real VM, check the collector version, eBPF loading, tool execution, and data export. Reuse an existing valid receipt if it covers identical content. Prefer targeted tests for short commands, sparse samples, and measurement retention on timeout; do not expand this into 503 additional performance experiments.
- Let standard qualification cover the necessary capability checks for each new template. Do not precede it with redundant full-input checks, full smoke tests, and full trace replays.
- If Full template-affinity requires a physical Base-sharing proof, changed image/template identities invalidate the old proof. Do not edit IDs in an old proof and reuse it. Generate a new proof through the existing `prove-template-sharing` path and preserve the raw evidence. First estimate the actual time required for this and qualification, and reuse evidence where its validity permits. Do not disable the mechanism or fabricate proof to meet the schedule.
- Prediction must use the current frozen bootstrap-v2 directory and its hash, with matching host and guest contracts. Put new writable state in a new directory instead of overwriting old training state. Do not assume an old calibration artifact is compatible with new templates based on its name, and do not start an unrelated large retraining exercise.

If preparation stalls, prioritize concrete problems that affect formal experiments. Record cosmetic logging issues or historical API compatibility issues that do not affect results and move on. Do not repeatedly retry the same build merely to fill the five-hour window.

## 7. Freeze the formal experiment definition and arrival process

Generate a new schema-4 configuration from the historical complete Full YAML, replacing the source/host/catalog/runtime/seed/proof identities for this study. Use a custom comparison with a single arm explicitly named Full. Export the final effective mechanism settings; the arm name alone does not establish that mechanisms are enabled.

Reference Full configuration: predicted Tool admission; managed deployment with demand accounting; load-aware placement with template affinity; pressure-driven shared borrowing; proactive memory reclamation, memory protection, and CPU release boost; the existing idle-reclaim policy; incremental-cow, tiered checkpoint storage, and value-based residency; measured-cost action selection. Honor the configured snapshot mechanism and do not silently fall back to another snapshot backend.

Historical settings: Tool 3072 MiB/2 vCPU; Runtime 1024 MiB/2 vCPU; pair create gate=10, which permits up to 20 concurrent Cube create requests with the current parallel pair implementation; borrow limit=100 GiB; borrow cooldown=0; pool transfer concurrency=4; worker thread switch interval=.001s. Start with values supported by evidence rather than sweeping multiple parameter combinations. If a parameter change addresses an identified bottleneck, save a new configuration and use a new run ID.

Important detail of the current Poisson implementation: `session_offer_offsets` multiplies the median case duration for the sessions assigned in that run by `poisson_arrival_window_fraction`, then divides by N-1 to obtain the mean exponential interarrival time; the first offset=0. Round-robin case frequencies differ between 1280 and 2560, so their medians may differ. Setting fraction=2.0 in both configurations does not guarantee a common window.

Minimal implementation:

1. Read existing pilot records and the recorded durations of the 503 unique traces. Use the earlier experiment's 2.0 times reference duration as the candidate common window. Record the selected W in seconds and the reason for choosing it. Do not launch multiple new performance pilots merely to choose W.
2. If the current CLI cannot specify an explicit window, add an optional public field with seconds as its explicit unit, such as `poisson_arrival_window_seconds`. Use fixed W when specified; otherwise retain the existing fraction formula. Document its allowed range, default, and requirement for a new run ID; no image rebuild is needed. Write only the necessary determinism and boundary tests.
3. Freeze the same order of 503 cases, W, random_seed, and time_scale=1.0. Save offsets and hashes separately for 1280 and 2560. Reuse identical offsets for before/after comparisons at the same scale. The actual final arrival time of the exponential process need not equal W exactly; do not rescale it to exactly W and thereby alter the arrival distribution.
4. Report expected λ=(N-1)/W, the actual offered-arrival curve, lateness relative to scheduled arrivals caused by the scheduling thread, and the actual peak active count. Do not remove delayed dispatch from end-to-end waiting time or reduce λ and present that as a scheduling optimization.
5. Do not change experiment semantics by pre-creating all VMs together or adding a barrier for all sessions. If warm-start behavior needs separate discussion, distinguish it explicitly in the report rather than mixing it into this run's cold-start E2E results.

## 8. Observability for both queues: fill gaps in the existing implementation

Reuse existing events and one-second aggregate sampling. Distinguish task states from tool-operation states; their counts are not interchangeable.

VM path: scheduled arrival → actual dispatch → pair-create gate wait → lifecycle memory-admission wait → actual Runtime/Tool creation → both VMs and their bridge ready → agent runnable. The creation gate and memory admission are separate waits and must be timed separately.

Tool path: model response produces an executable call → dependency gate/installation → tool-admission queue → memory_prepare/borrow/restore → admission granted → actual guest start → guest end → result delivery and reservation release.

For every state transition, record run/attempt/session/case/execution IDs, queue/stage, host monotonic time, correlation sequence, domain, and outcome. Record enqueue/grant/abandon/failure events and changes in blocking reason with their duration, rather than logging every poll. Necessary reasons include LOCAL/total budget, create gate, same-session conflicts, sampling in flight, transfer concurrency, borrow eligibility/cooldown, restore wait, and dependency gate.

Output the following once per second:

- Total, offered, not-yet-offered, currently active, completed, and failed sessions. Distinguish cumulative activations from currently active sessions.
- Tasks waiting at the VM gate, tasks waiting for lifecycle admission, pairs/VMs being created, oldest wait, and activation rate.
- Tasks waiting for the LLM, with recorded expected wait and actual wait overshoot reported separately.
- Ready operations and distinct sessions waiting for tool admission. Separately count admitted-but-not-started and executing operations/sessions.
- The current longest queue wait, age distribution of requests still waiting for admission, and enqueue/dequeue rates. Do not report only successfully dequeued requests while omitting requests that have waited hundreds of seconds.
- Control-plane/VM CPU usage, CPU PSI, actual LOCAL use and reservations per NUMA node, shared-live use, occupied/reserved WARM capacity, and candidate/selection/completion reasons for memory actions.

Currently, `ArmActivityTracker` marks a session activated when the runtime emits `sandbox_created`. With parallel pair creation, this does not prove the Tool VM is ready. Verify and correct this, or explicitly add pair_ready/agent_ready metrics. A session may have an LLM wait and other tool operations at the same time. Use operation counts and an explicit treatment of overlap; do not add these sets together and call the result the total task count.

Currently, `EventWriter.write` holds a lock while opening the file, serializing JSON, flushing, and closing for every event. This is a concrete hotspot candidate to measure, not a confirmed root cause. If measurements show material overhead, reduce hot-path I/O with a persistent file handle, batched writes, or bounded asynchronous writes. Capture event occurrence time at the producer, preserve complete lifecycle records, flush on exit, and make write failures visible. The observer must not hold up the admission condition. Do not scan every cgroup at high frequency for logging. Measure logging throughput, write-lock wait, and CPU cost so observability does not create a bottleneck.

Do not directly subtract uncalibrated guest and host timestamps. Use host send/receive times and durations measured within the guest. Retain intervals that cannot be decomposed precisely, and do not misclassify notification/network latency as guest execution time.

## 9. Core metrics: overhead and the critical path

For every session, record scheduled arrival, actual dispatch, pair readiness, the first model request, each LLM/tool event, final response, validation completion, and destruction completion. Define primary E2E as scheduled arrival to final response. Separately report total elapsed time through validation/cleanup. Check the endpoint of the existing `offered_to_completion_seconds` metric before using it; renaming the field is insufficient.

Report per session:

- E2E; recorded LLM wait; actual LLM wait and overshoot; actual tool execution; other waiting/control time.
- Separate durations for the VM gate, lifecycle admission, VM service/guest startup, dependency wait/installation, Tool admission, memory_prepare, admission-to-start, end-to-result, validation, and cleanup.
- Overhead in seconds, as a percentage of E2E, and as a percentage of elapsed time after subtracting recorded LLM wait. Preserve the scheduled LLM time; do not silently shorten replay sleeps during optimization.

Do not sum the execution durations of concurrent tools in one session and subtract that sum from E2E. Use interval unions to establish wall-clock coverage and overlap of tool-execution/LLM intervals, and retain causal relationships when constructing the critical path. Decompose waits along the serial critical path. Adding stage p90 values does not yield end-to-end p90. Label incompletely decomposed cross-component intervals as unresolved residuals rather than inventing attribution to a control cost.

Provide both perspectives: queue/service distributions per tool request and E2E/critical-path distributions per session. Add the missing p90 to the report; the worker currently reports only p50/p95/p99 for offered-to-completion. Show p50/p90/p95/p99 together with sample counts and failed/cancelled/incomplete counts.

The claim that tool execution time does not increase with concurrency is a hypothesis to test. Compare CPU, I/O, memory pressure, and observed service time within matching case/command categories so tool slowdowns are not misattributed to the control plane. No new set of 503 single-concurrency baselines is required. Answer the question using before/after measurements under the same workload, existing comparable records, and stage timings from this study.

Report successful tool steps/s, successful sessions/s, offered-arrival rate, execution-phase throughput, and whole-run throughput including queue drain separately. Make time windows and denominators explicit. Do not inflate throughput with early failures or report only the best short interval.

If the only pre-fix runs were cancelled, matching stages/comparable load intervals may be used for diagnostic comparisons, but disclose truncation and survivorship bias. Without a comparable complete pre-fix result, do not claim a complete E2E speedup ratio. There is no need to rerun a known unhealthy full baseline solely to obtain that number.

## 10. Diagnosis and repair priorities

Use the stage distributions and queue timeline from formal Full-1280 to identify the longest avoidable waits, then sample the relevant processes/threads. Collect short py-spy/perf/thread-stack samples using tools available on the host. Do not first install a large observability system.

P0: Correct results and forward progress. Check whether admission releases are blocked by locks/sampling, sibling tool reservations in the same VM wait on each other, failure/cancellation paths omit releases, zero-reservation paths omit events, partial parallel-pair failures leak VMs, or event statistics undercount tasks still waiting. Prioritize targeted concurrency tests for these concrete invariants.

P1: Confirm that existing fixes take effect. Check for remaining slow exact-sampling paths inside the global condition, O(N²) work per feasible-prefix evaluation or wake-up storms, shared transfers blocking all admission progress, and slow VMs/infeasible head requests affecting other domains. Do not reimplement the existing aggregate-claim algorithm or introduce another scheduler.

P2: Control-plane hot paths. Where profiling points to them, address GIL/thread scheduling, repeated per-tool process launches/configuration reads/prediction queries, synchronous log I/O, repeated cgroup scans, HTTP/SSH connection setup, lock granularity, and delayed completion callbacks. Prefer caching immutable data, reusing connections, and moving slow I/O out of critical sections while preserving error propagation and experiment semantics.

P3: VM creation. Separate gate wait, Cube API time, cgroup pinning, guest readiness/collector compilation, and Runtime startup. Earlier runs encountered cpuset_post_attach/kernel workqueue stalls. If they recur, collect blocked stacks and concrete triggering conditions instead of merely increasing timeouts. Start with a create gate of 10; adjust only when evidence shows the service is not saturated. Do not blindly raise it to 50 and create kernel contention.

P4: Why shared-pool use is low. Correlate each wait with LOCAL watermarks, predicted demand, eligible borrowing candidates, free shared capacity, transfer slots, cooldowns, cost ranking, and actions in progress. Distinguish no memory pressure, no eligible candidate, an eligible candidate incorrectly rejected by a condition, slow action execution/completion notification, and a bottleneck in CPU/control-plane/VM creation. Diagnose a sharing-scheduler problem only when eligible demand exists and capacity/constraints allow progress, yet progress does not occur. Do not force borrowing or lower the LOCAL budget merely to increase pool utilization.

For every fix, retain at least: triggering evidence → cause → smallest sufficient change → targeted correctness check → whether the corresponding wait decreases in a new run. Consult the user before changes to policy definitions, learning semantics, or prediction mappings. Fix ordinary implementation defects within the existing authorization.

## 11. Five-hour schedule and stopping conditions

This is a time budget, not a promise that all replays will finish. Record the start time and absolute deadline. Read-only analysis and local tests may run while environment updates proceed, but stop builds, qualification, and other heavy work for this task during formal measurements to avoid interference.

| Elapsed time | Main work | Expected deliverable |
|---|---|---|
| 0–15 min | Inspect live state/source/host differences; start the minimal environment update path | Frozen source, host plan, environment-update time estimate |
| 15–75 min | Batch collector/template updates and necessary proof/qualification; analyze old logs offline and add missing timing fields in parallel | New catalog/seed/proof identities and correctness results; no additional single-session or performance ramp-up experiments |
| 75–110 min | Go directly to Full-1280 as soon as preparation passes; inspect queues and control-plane hotspots live | First measured breakdown for the current code and the principal blocker |
| 110–175 min | Fix the 1–3 highest-impact problems; if needed, stop unhealthy runs through the supported CLI and restart with new IDs | Fix evidence, targeted tests, and a new formal run |
| 175–250 min | Aim to complete a healthy Full-1280, then proceed to Full-2560; change only scale and the corresponding frozen offsets | Formal results at both scales, or explicit completed/incomplete status |
| 250–275 min | Reserve time for healthy experiments already finishing and for offline analysis; do not start another long experiment | Critical-path/queue/resource plots and statistics |
| 275–300 min | Clean up owned resources, preserve all evidence, summarize results and remaining issues | Reproducible handoff and cleanup audit; no indefinitely running background task |

If preparation is unfinished at 75 minutes, revise the remaining budget immediately. Do not start on a broken environment, silently revert to the old collector, or substitute a single-task experiment for the 503-task workload. Reduce redundant checks and nonessential polish first. Report exactly which build/materialization/proof stage is consuming time. Use the remaining time for feasible formal work without restarting the five-hour clock.

Stop an unhealthy run early for a clear deadlock/sustained lack of progress, resource-budget violations/OOM, task-semantic failures, widespread environment errors, or a logged blocker known to be unrecoverable. Idle CPU with continuously increasing queue age is a signal to collect evidence immediately; one second of low CPU usage alone is not grounds for cancellation. Do not repeatedly restart healthy runs for minor changes.

The old “more than 10 minutes to activate 1280” observation is a diagnostic red flag. Under Poisson arrivals, account explicitly for the scheduled arrival span and distinguish cumulative readiness from simultaneous activity. Do not turn it into a requirement that all tasks remain resident simultaneously. If a formal 2560 run is unlikely to finish within the remaining time, clearly labeled truncated diagnostics are acceptable; they are not complete throughput/p90 acceptance results.

Reserve at least the final 25 minutes for cleanup and delivery. Do not retain the old 18000-second timeout in a way that leaves work running unattended beyond the five-hour deadline. Check remaining wall-clock time and the cleanup budget before each run. End expired runs through the supported abort command, preserving outstanding queue ages and failure/cancellation information.

## 12. Supported commands and the run package to produce

Use scripts from the new source snapshot and load an explicit machine.env. The executor must set the variables below from the saved configuration and record their values in the launch record. They are not an invitation to guess template IDs or reuse an old profile without checking it.

```bash
# Run from this study's frozen source root; obtain MACHINE, HOST_PLAN, SPEC, RESULTS, and RUN_ID from its manifest.
bash scripts/clawbox --machine-env "$MACHINE" experiment host inspect
bash scripts/clawbox --machine-env "$MACHINE" experiment host check "$HOST_PLAN"
bash scripts/clawbox --machine-env "$MACHINE" --sudo experiment host apply "$HOST_PLAN"
bash scripts/clawbox --machine-env "$MACHINE" experiment describe "$SPEC"
bash scripts/clawbox --machine-env "$MACHINE" --sudo --output-root "$RESULTS" experiment launch "$SPEC" --run-id "$RUN_ID"
bash scripts/clawbox --machine-env "$MACHINE" --output-root "$RESULTS" experiment status "$RUN_ID"
bash scripts/clawbox --machine-env "$MACHINE" --output-root "$RESULTS" experiment report "$RUN_ID"
# Run only when this run must be stopped; configure the existing cleanup options for the remaining time.
bash scripts/clawbox --machine-env "$MACHINE" --sudo --output-root "$RESULTS" experiment abort "$RUN_ID"
```

`launch` performs qualification or reuses a matching receipt. There is no need to invoke qualify separately and then repeat the same checks. If qualification is performed separately in advance, ensure launch actually reuses it. Check the current CLI help for argument syntax first. Changes to images/templates/schema cannot reuse a receipt that no longer matches.

If environment refresh or a fixed Poisson window requires new scripts/options, implement them first and then record the actual runnable commands in the handoff run package. This plan does not invent commands that do not yet exist. Document parameter units, defaults, and requirements for image rebuilds/host apply/new runs in user documentation; keep debugging timelines in the evidence directory.

The run package must contain at least:

```
manifest.json / decisions.json / deadline.json
source/ + source-hashes.json + working-tree.patch
host-plan.yaml + host-profile.json + host-check/apply receipts
environments/catalog.json + collector-update receipts + sharing proof
workload.json + trace hashes + seed identity
full-1280.yaml / full-2560.yaml + arrival-offsets-*.json
runs/<unique-id>/...  # Native CLI result directories; never overwrite manually.
analysis/latency-breakdown.json + queue-timeseries.csv + plots/
report.md + next-steps.md
```

## 13. Questions the final report must answer

1. Which specific bugs or implementation inefficiencies caused long waits before tool execution or after completion? Give the code path, triggering conditions, fix, and changes in stage durations.
2. How many tasks were in the VM queue, Tool queue, LLM wait, and admitted-but-not-started state? Where were the longest waits and outstanding requests?
3. How much E2E time was actual tool execution, recorded LLM time, and overhead? Provide p90 and absolute seconds, and explain overlap and critical-path accounting.
4. How did offered-arrival rate, successful throughput, and excess waiting change from 1280 to 2560? Did CPU, LOCAL memory, or shared memory become a bottleneck?
5. Was low shared-pool use caused by no demand, no eligible candidates, a scheduling bug, or another bottleneck? Provide event-level evidence, not just utilization curves.
6. Which Full mechanisms actually triggered, and which were enabled but did not trigger? Were the budgets of 160 CPUs, 1000 GiB LOCAL, and 100 GiB shared memory maintained?
7. Which findings come from complete real-VM experiments, and which come from qualification, unit tests, or cancelled diagnostic runs? What remains untested or unfinished because of the five-hour limit?
8. How can the next session reproduce, recover, and clean up using the saved commands/configuration? Are there zero VMs remaining that belong to this run?

The deliverable should explain reproducible performance causes and improvements. It should not become a lengthy debugging narrative or a production-grade monitoring platform for a paper prototype.
