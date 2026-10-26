# Full service-overhead review and independent-session handoff

Reviewed 2026-10-26, Asia/Shanghai. This is an evidence review and repair plan, not a claim that the optimizations are finished. Read `AGENTS.md` and `FULL_LATENCY_HANDOFF_20261025.md` for experiment policy. The user's latest scope is: **reduce work and implementation stalls that take time themselves; waiting for free memory is not an optimization target in this review.**

## 1. What to solve first

Substantial overhead remains after excluding memory admission. The strongest actionable mechanism is long NUMA migration holding a borrower-wide lock needed by unrelated restore placement checks. Startup is also severely slow; one important startup timer was missing from the previous high-level breakdown. Native create/checkpoint/restore calls and repeated per-tool measurement remain expensive.

Recommended order:

1. Separate startup SSH worker-queue delay from actual command/retry time, and split sidecar startup into its real phases. This is the largest unresolved startup interval and needs only small timing additions, not another framework.
2. Remove unrelated-VM serialization around NUMA migration and placement bookkeeping, preserving same-VM serialization and reservation correctness. At the same time profile the slow migration syscall itself.
3. Use Cube's official pre-warmed service-template pattern where initialization can safely be reused; preserve independent per-session ClawTune state and measurement semantics.
4. Profile the remaining native API time and per-tool sampling under the next large run. Do not rework logging, prediction lookup or CPU placement without new evidence: their measured hot-path costs are already small.

There is no complete successful Full-2560 result establishing that the current checkout is fast or bug-free. Do not call these issues solved merely because tests pass or a single VM restores quickly.

## 2. Evidence and exact timing definitions

Primary diagnostic is `full-c2560-latency-final24-20261026T1340cst`, captured at `2026-10-26T06:10:43.348516Z`. It was cancelled: 2,560 dispatched, 764 pairs ready, 753 agents runnable, 147 failed sessions, 2,413 incomplete, zero validated complete sessions, one observed final response. These are observed-call distributions, subject to unfinished-operation and cancellation bias.

All times below are seconds. Numbers are from preserved native events, not configuration estimates. Lifecycle counts include Runtime and Tool VMs; per-tool rows explicitly use `agent-tool` scope.

| Measured interval | n | p50 | p90 | p99 | Interpretation |
|---|---:|---:|---:|---:|---|
| Sidecar readiness after setup | 754 | 25.739 | 46.767 | 63.884 | Application startup plus health-command transport/polling; no free-memory admission inside this interval |
| Restore, subtracting that operation's own transfer-slot wait | 796 | 9.169 | 24.122 | 32.775 | Native restore + placement/control; still includes implementation lock stalls |
| Restore API wrapper | 796 | 6.497 | 12.576 | 20.723 | SDK/server/native work, transport and retry; not pure hypervisor CPU time |
| Restore `pin_local` | 796 | 0.011 | 16.552 | 21.591 | Placement verification/bookkeeping, including borrower lock acquisition |
| Borrow rebind service | 33 | 1.305 | 16.561 | 25.389 | Migration helper service, excluding measured transfer-slot wait |
| Borrow page-migration phase | 33 | 0.806 | 16.032 | 25.030 | `numa_migrate_pages` interval |
| Return-to-LOCAL rebind service | 17 | 9.952 | 16.449 | 21.172 | Migration helper service, excluding measured transfer-slot wait |
| Checkpoint, subtracting that operation's own transfer-slot wait | 828 | 6.750 | 15.152 | 23.955 | API plus checkpoint accounting/observation work |
| Checkpoint API wrapper | 828 | 5.759 | 9.263 | 13.691 | Pause/snapshot SDK wrapper; includes confirmation and native work |
| Create API wrapper | 1,528 | 3.551 | 9.436 | 15.124 | After admission/transfer acquisition, not the VM creation backlog |
| Runtime setup command | 754 | 4.441 | 9.477 | 14.324 | Per-session files/state initialization and sidecar process launch |
| Agent-tool memory-baseline binding | 6,769 | 0.008 | 1.353 | 3.695 | Repeated memory sampling/binding; not free-memory waiting |
| Agent-tool lifecycle-lock acquisition | 6,769 | <0.001 | <0.001 | 16.291 | Tail contention; not a typical 16-second cost on every call |
| Post-restore Tool bridge readiness | 388 | 0.558 | 2.133 | 2.833 | All observed calls succeeded on first attempt |
| Admission-phase SSH identity check | 386 | 0.518 | 2.353 | 11.093 | All observed calls succeeded on first attempt |
| Destroy lifecycle | 292 | 2.374 | 20.222 | 29.931 | Usually after final response; can delay resource release for other sessions |

The aggregate restore p90 is 31.578 s; restore transfer-slot wait p90 is 19.209 s. **Do not subtract those percentiles.** The 24.122 s above was calculated as `service_seconds - transfer_slot_wait_s` on each of the 796 matching events and then quantiled. Likewise checkpoint was computed event by event. Restore API plus placement on the same event has p90 23.871 s, showing where almost all of the measured non-slot restore interval resides.

We exclude VM creation-gate waiting, placement capacity waiting, tool admission-blocked intervals, and external restore reservation waiting from the service ranking. Transfer-slot queues are also reported separately rather than called execution service. A mutex wait created by one slow migration blocking unrelated VMs remains an implementation overhead worth fixing; it is not waiting for free memory.

Do not label `tool_control.memory_prepare` p90 24.673 s (agent scope) or total tool-queue p90 27.378 s as pure overhead. They mix reservation waits with controller work. Subtracting their aggregate p90s is invalid. Instrument actual condition/queue/lock intervals if that composite path becomes the next target.

## 3. Startup: a missing large interval, and slow sidecar initialization

### SSH startup readiness needs a timing split before attribution

The `native_ssh_identity_ready` startup events show:

| Startup interval | n | p50 s | p90 s | p99 s |
|---|---:|---:|---:|---:|
| Submission-to-observed-completion, currently named `service_seconds` | 754 | 78.567 | 157.300 | 187.813 |
| Blocking remainder when startup joins the future | 754 | 50.534 | 123.711 | 162.111 |

This **does not prove 157 s of SSH execution**. `worker.py:begin_tool_telemetry_warmup` records its start before submitting to a shared `ThreadPoolExecutor(max_workers=min(64, session_worker_count))`. `await_tool_telemetry_warmup` records completion when the session joins the future, not at the worker's return. Thus the named service timer includes executor queueing and potentially time after the future already completed. The blocking remainder is a real startup delay, but still mixes worker queueing, retries and command execution. It is not free-memory admission.

`warm_runtime_ssh_route` runs the strict identity command through `runtime_executor.execute(command, 30)`, retries with 0.5 s sleeps, and permits up to 120 attempts. Its SSH command has `ConnectTimeout=2`. Startup attempt count p50=2, p90=3, max=10. Two-second SSH connect timeout does not bound SDK request, command launch, output collection or the entire warmup. It would be wrong to infer that 0.5 s polling explains the whole delay.

First patch: return/record monotonic submit, worker-start and worker-finish timestamps, plus per-attempt execute duration, exit status/error and retry-sleep duration. Keep producer event occurrence times. Distinguish `executor_queue`, `warmup_execution`, and `join_blocking`. Do not double-count work overlapped with sidecar readiness. If worker queueing dominates, first fix slow occupied workers; blindly increasing 64 workers can overload Cube and does not prove service improved. Preserve the SSH host-key/session-identity check: accepting a stale endpoint could execute a task in another VM.

Code: `clawbox/experiments/worker.py` (`native_ssh_identity_command`, `warm_runtime_ssh_route`, warmup pool near line 2299, startup callbacks near line 4198); `clawbox/experiments/openclaw_driver.py` (`startup_begin`, `startup_wait`); `clawbox/cube/executor.py` and the client's command execution path.

### Sidecar readiness is not VM creation

`openclaw_driver.py` launches a new `python -m clawtune_sidecar.main` for each session after writing keys, launcher scripts, prediction manifest and mutable state. The next command polls relay readiness when applicable and `/health/ready`, with 0.5 s sleeps and 2 s curl limits. Sidecar readiness p90=46.767 s and setup p90=9.477 s are separate from VM create API time. Their percentile values cannot simply be added.

In the current local ClawTune source, `create_app()` calls `build_state()` before serving, and the Uvicorn startup callback awaits `tool_monitor.prepare()`. That path includes predictor/KB initialization and collector preparation. These are concrete phases to time, not proof that imports, KB loading or eBPF compilation specifically account for the 47 seconds. The local ClawTune checkout is not proof of the code baked into the recorded Runtime template.

Instrument process start, module import completion, state build, collector preparation, listener-ready and successful health observation. Record durations inside the guest; use host RPC send/receive bounds instead of subtracting uncalibrated guest/host clocks. Inspect a small sample of slow and fast sessions under the same load. Do not disable required measurement merely to make readiness return earlier.

**Official reuse opportunity:** Cube documents snapshotting an already initialized resident application after its real readiness probe succeeds. Use that pattern for reusable initialization rather than rerunning all startup work after every create. Apply only after identifying the expensive phase. Each session still needs its own writable ClawTune state, owner, trace destination and credentials. Do not bake session data/secrets or reuse stale external sockets. A sidecar already bound to one session cannot simply be cloned unchanged. Rebuild only the affected Runtime template if that is the actual changed artifact; inspect packaging before proposing any rebuild of the 503 task environments.

Official guide checked during this review: [Pre-warm a Service in a Template](https://docs.cubesandbox.com/guide/tutorials/prewarm-template-service). This describes a reusable design, not a measured latency guarantee for our ARM host or 2,560 offered sessions.

## 4. NUMA migration blocks unrelated restore control

### Verified lock mechanism

`SandboxNumaBorrower.borrow()` holds `self._lock` while calling `_move()`. `return_local()` does the same. `_move()` includes external migration work. `pin_local()` also takes `self._lock`; so do bookkeeping/read paths such as `borrowed()` and `residency()`. `release_destroyed()` can perform reclaim writes while holding it. The borrower is shared within its placement domain, so unrelated VMs using that borrower contend.

`pin_local()` normally checks cpuset policy and records already verified placement; it is not supposed to copy gigabytes of RAM. Current code already reuses `verify_cgroup` observations and moves discovery outside the borrower lock. Preserve those changes. The distribution is revealing: median 11 ms but p90 16.552 s. Slow migration holding a shared lock can explain a multi-second tail. **The lock structure is verified; how much of each recorded 16-second pin interval is lock waiting is not yet directly measured.** Kernel cpuset writes/reads remain another possible contributor.

Smallest repair direction: use short shared-state lock sections to reserve/inspect/publish state, serialize operations on the affected VM separately, and do slow migration/reclaim outside a domain-wide bookkeeping lock. Revalidate VM identity/generation before publishing. Preserve the existing pool-wide migration serialization unless evidence demonstrates that changing it is safe; lock isolation alone need not allow multiple simultaneous migrations. Keep uncertain migration reservations charged and isolate the affected VM instead of failing all domains.

Add timers around borrower lock acquisition/hold, `_leaf_for`, cpuset reads/writes, residency reads, and external migration. A targeted concurrency test should block migration for VM A and prove a read/placement check for unrelated VM B can progress, while conflicting operations on A and its reservation publication remain serialized. Also cover failure after migration starts. These are correctness tests; the speed claim requires the next large run.

Code: `clawbox/experiments/numa_borrow.py:428` (`borrow`), `:439` (`pin_local`), `:532` (`return_local`), `release_destroyed`; `clawbox/cube/lifecycle.py:699` (`restore`); `clawbox/experiments/snapshot_pool.py` (transfer slots and `rebind_lock`). Existing tests: `tests/test_numa_borrow.py`, `tests/test_cube.py`, `tests/test_snapshot_pool.py`.

### Migration itself is still expensive

Borrow events give a direct internal decomposition (n=33): migration p90 16.032 s, cpuset phase 0.350 s, native preparation 0.164 s, native resume 0.021 s. Rebind service p90 is 16.561 s. Transfer wait p90 is approximately 15 microseconds. Return migration p90 is 16.140 s (n=17). This is not predominantly queue waiting or VM resume.

`numa_rebind.py:rebind` asks the native shim to pause/detach guest memory, calls `libnuma.numa_migrate_pages`, then uses threaded cpuset children to apply policy without the earlier populated-cgroup attachment stall, restores thread affinity and resumes. The slow phase is the migration syscall interval. Its internal cause still requires kernel stacks/perf and page-volume evidence: raw copy bandwidth, page scanning, kernel locks, reclaim and invalidation are different mechanisms.

For one concrete saved borrow, migration took 16.704 s while observed shared residency grew by about 342 MiB and LOCAL fell by about 348 MiB. Those are before/after residency deltas, **not measured bytes copied** (`migrated_bytes` is null). Do not divide configured 3 GiB capacity or residency deltas by elapsed time and present that as migration bandwidth. Log residual pages and actual helper phases; capture a short stack sample during a slow call. Check that native detach is in effect before proposing another implementation of the same fix.

Do not remove pause/RAM detach, dirty-bit preservation, thread-affinity restoration, residual shared accounting or uncertainty isolation to obtain a faster number. Do not merely increase the helper's timeout. Cube lifecycle pause/resume or cross-host migration is not automatically equivalent to live borrowing into NUMA 2 on this host.

## 5. Native API costs: restore, checkpoint and create

Current `CubeSandboxLifecycle.restore()` measures: physical observation, cgroup preparation, transfer-slot acquisition, `client.connect_sandbox`, cgroup verification, `pin_local`, retained snapshot accounting and fault observation. External restore memory admission occurs outside this measured service interval. Transfer slots are released before placement checks.

`client.connect_sandbox()` calls SDK `Sandbox.connect` through a retry wrapper. Consequently `api_restore_s` includes HTTP/SDK retry and server-internal work; it is not a pure hypervisor restore measurement. Checkpoint likewise wraps SDK pause/confirmation and direct snapshot metadata handling. Current create already uses native Cube creation, concurrent Runtime/Tool pair startup, and native placement. Switching to the same public API again is not a new optimization.

Next measurement should correlate sandbox ID and operation occurrence with CubeMaster/Cubelet/shim timings: request receipt, internal dispatch, container/task setup, cgroup operations, memory-map restore, interrupt-controller restore, device restore, process/network readiness, response and retry. Existing historical native IRQ/device records are leads, not evidence that today's API time has the same cause. Preserve IRQ routing batching and masked-vector handling already implemented; inspect the installed native source and binary provenance before rebuilding anything.

Historical artifact `analysis/irqbatch-native-stages-terminal.json` contains interrupt/device timings from an earlier diagnostic; do not mix its population with final24 events. An earlier small real-VM qualification reported roughly 0.2 s restore, but that is not a promise under hundreds of active VMs. Measure representative working sets and load before setting a target.

Official [lifecycle documentation](https://docs.cubesandbox.com/guide/lifecycle) uses `pause()` and `Sandbox.connect()` for pause/resume. [Snapshot/rollback/clone](https://docs.cubesandbox.com/guide/snapshot-rollback-clone) provides different operations; it does not establish that snapshot creation releases source VM memory or replaces our shared/WARM accounting. Keep the configured `incremental-cow` mechanism, lazy retention, tiering and shared budget. Check installed-version support before adopting newer official functionality. A version upgrade is a new comparison baseline, not an invisible change mid-run.

Do not tune the snapshot semaphore first to hide slow service: final24 used 32 overall transfer slots and 24 snapshot slots. Slot waiting is a separate control queue. Increasing it may worsen native contention. After fixing service costs, inspect sustained completion rate and queue length to decide whether those limits need adjustment.

## 6. Repeated per-tool control and secondary costs

The cleanest repeated measured cost is `tool_control.memory_baseline`: agent-tool p90 1.353 s, p99 3.695 s, across 6,769 admissions. Backend maintenance is separate (19,056 admissions, baseline p90 1.673 s). Do not mix the two populations to describe agent calls.

`worker.py` records this around `coordinator.bind_tool_memory(session_id)`. `policy.py:bind_tool_memory` already performs the kernel sample outside the global admission condition and keeps the nominal claim charged while reading. Preserve that fix. Determine whether time is in `memory.numa_stat`, cgroup observation, reacquiring the condition or another callback; avoid reintroducing per-claim scans or exact reads under a global lock. If sampling is cached/coalesced, retain conservative reservation accounting and preserve ClawTune's measured values. Missing or zero-size predictions must not bypass execution or disappear from event counts.

Agent lifecycle-lock p99 16.291 s is another tail signal, but p90 is below a microsecond. The per-session lock also protects checkpoint/restore, route recovery and memory binding. Log the holder's operation to distinguish legitimate same-VM serialization from unnecessary control blocking. Do not label it a global GIL problem without stacks.

Post-restore bridge and identity service p90s of 2.133/2.353 s also remain; their observed calls all needed one attempt, so retries cannot explain those distributions. Trace the SDK command/transport path before changing retry policy.

Lower priorities: logging lock p90 is below a microsecond; prediction/queue bookkeeping is roughly 70 microseconds at p90; CPU placement is microseconds. Destruction p90 20.222 s can delay release, but generally lies after the final-response E2E endpoint. Old Full-1280 completion-notification-to-completed p90 was 4.352 s; an equivalent current2560 distribution is missing, so do not claim it is fixed or remains that exact size.

## 7. Current source, preservation constraints and host status

Local ClawBox tracked tree was clean at review start; HEAD `2a116a9`. Local sibling ClawTune was clean at `9f56cb2`. This handoff and diagnostic output are the only work in this review; no performance patch, new VM or experimental rerun was made.

Recent ClawBox commits to retain and review before editing:

| Commit | Purpose |
|---|---|
| `2a116a9` | Retry transient Cube endpoint lookup failures |
| `8358216` | Isolate incomplete NUMA rebinds |
| `bb5c6dc` | Prevent cgroup reclaim from blocking destroy |
| `d28aa0d` | Reconcile completed pause responses |
| `320d472` | Bound snapshot waves without throttling VM creation |
| `de25ac4` | Full orchestration and Cube startup latency optimizations |
| `fa49452` | Earlier Full latency/recovery optimizations |

Other existing behavior to preserve: official endpoint discovery, cached routes with identity checks, native file notification/status polling, parallel pair creation, stable preallocated cgroups, reused placement observations, conservative demand aggregation, sampling outside global locks, native IRQ/device fixes, incremental snapshots and NUMA KVM detach. Inspect source rather than assuming commit titles prove large-scale success.

The last endpoint retry patch was tested locally/remotely in the preceding work but has no completed Full validation. The final24 trace predates that retry patch. Neither a retry fix nor failure isolation proves a lower service cost. Do not reset blindly to an old purported bug-free baseline; no successful Full-2560 baseline has been established.

Host `ssh kunpeng` timed out during this review (`193.124.7.2:22`). Current running processes, VM inventory, native binary and remote edits could not be verified. Historical remote locations:

- Checkout: `/home/weitianc/ClawBox`, Python `.venv/bin/python`.
- Study: `/data/clawbox-full-latency-20261025T035151CST`.
- ClawTune source: study `source/ClawTune`.
- Native candidate source: study `diagnostics/cubesandbox-native-v2-src`; diagnostics/backups under `diagnostics/numa-kvm-reset`.
- Remote Git base was previously `d42da58` with overlaid source; compare source/patch records, not just remote HEAD. This is historical and must be refreshed.

Earlier cleanup removed three identified orphan shims belonging to final24 after the API inventory was empty. Sixty-five unrelated paused sandboxes were preserved. The next Full-1280 launch named `full-c1280-latency-dnsretry-20261026T1359cst` had been blocked before run creation; it is not a completed or running1280 result. These are prior-session facts, not a fresh host audit. Do not kill all paused VMs, remove templates, touch `anns`, or repeat broad cleanup. Verify ownership through the run ledger and process/cgroup identity before any cleanup.

Qualification is not a reason to rebuild everything: `require_host_ready` checks the physical VM domain before receipt validation. The earlier nonempty-domain failure did not prove the receipt had expired. `implementation_digest()` is a semantic contract marker, not an automatic hash of every source edit. Use the existing launch/receipt rules; do not add hashes, requalify twice or rerun 503 baselines for this performance task.

## 8. Evidence paths and reproduction

Local workspace: `C:/Users/29068/Desktop/ClawBox`.

Define `POST=.runtime/full-latency-execution-20261025T035151CST/analysis/post-runs`:

- Recent2560: `POST/full-c2560-latency-final24-20261026T1340cst/snapshot-20261026T061043.348516Z`.
- Earlier1280: `POST/full-c1280-latency-nativewatch-20261025t182251utc/snapshot-20261025T185050.073023Z`.
- Within each: `native/experiment.yaml`, `native/run-state.json`, `native/qualification.json`, `native/attempts/<attempt>/<arm>/events/*.jsonl`, `owned-sandboxes.jsonl` and the native evidence archive.
- Within `analysis/`: `stage-distributions.csv`, `stage-distributions-by-scope.csv`, `stage-observations.csv`, `queue-timeseries.csv`, `latency-breakdown.json`, `report.md`, `errors.csv` and operation/session observations.
- Recent2560 attempt: `attempt-830d6e1c652d4bd1`; arm: `04df583732ad2b550a12cb83`; owner: `clawbox-732d0b370f2bdcff73e8`.
- Earlier1280 attempt: `attempt-b5b48fdc5be8496e`; arm: `f5b72c51995d64c7e5c0aad3`.

Review-generated analysis: `.runtime/review_service_overhead_20261026.py` and `.runtime/service-overhead-review-20261026.json`. The script uses only the Python standard library, reads the preserved files, and writes a separate derived result. It calculates per-event transfer-subtracted costs, separates startup/admission identity checks and retains original stage/scope distributions.

```powershell
# Run from the ClawBox workspace; no VM or network needed.
python .runtime/review_service_overhead_20261026.py
git show --stat 2a116a9
git show --stat 8358216
```

The script handles Windows long paths. `.runtime` is ignored by Git: a session on another machine needs the evidence directory or native archives copied separately. This handoff embeds the main results so its conclusions remain readable without that copy.

Earlier1280 is a different cancelled diagnostic, with 13 validated complete sessions and 16 final-response events. Its restore-without-slot p90 was 25.992 s, create API 15.530 s, sidecar readiness 9.680 s and startup SSH blocking remainder 8.152 s. These are diagnostic contrasts, **not controlled1280-to2560 scaling ratios**: load, source and observation coverage differ. Do not reuse the much earlier two-session 72.64% overhead estimate as a decomposition of the recent run.

Existing timeline artifacts under `.runtime`: `full1280-task-state-memory.png`, `full2560-task-state-memory.png`, `full1280-2560-cpu-shared-timeline.png`, `full1280-2560-checkpoint-queue-timeline.png`, with associated CSVs and plotting scripts. They are useful for load windows, not exact service attribution. Checkpoint queue/service splitting was estimated from completed-event durations; unfinished actions are not fully represented. VM-cgroup CPU utilization excludes control-plane CPU. Shared live residency is not exact borrow-only bytes; snapshot allocation and reservations must be distinguished from actual residency.

## 9. Execute the next session efficiently

1. Read this handoff, the listed source functions and recent commit diffs. Reproduce the local JSON. Refresh host/process/owned-VM/source status once SSH returns. Do not infer that the host is idle from this review's connection failure.
2. Make the small timing split and lock-scope repair together where supported by code review. Keep live same-VM transitions and reservations correct. Add only targeted concurrency tests and necessary VM integrity checks. Do not build a monitoring platform or repeat whole installation acceptance.
3. For startup, inspect the actual Runtime image's initialization phases. Reuse official template prewarming for proven reusable work; avoid a broad ClawTune architecture rewrite. Only rebuild changed artifacts. Public options, if added, need the scenario walkthrough/reference updates required by `AGENTS.md`.
4. Use the existing `clawbox experiment` launch path with a new unique run ID. Latest user direction was1280 instead of320, then2560. Small checks are correctness gates, not performance evidence. Combine minor fixes with the next consequential large run rather than restarting solely for corner bugs.
5. Preserve 160 experimental CPUs (0-159), LOCAL 1000 GiB capacity with 960 GiB high-watermark/admission budget, and one shared100 GiB budget for live borrowing and WARM snapshots on NUMA2. Keep bootstrap-v2, measured replay waits, per-session writable environments and snapshot mechanism. Never enlarge resources or shorten LLM waits to manufacture improvement.
6. Original plan requires a common expected Poisson arrival window across scales. Final24 used 741.1812062263489 s. Inspect the pending1280 spec: an earlier preparation used a different proportional window. Resolve that inconsistency in the frozen specs before treating runs as comparable; don't silently compare different arrival definitions. Keep seed/offset evidence and every run immutable.
7. During the run collect a short sample of worker/native stacks only when the measured slow phase occurs. Report service p50/p90/p99 and count, failure count, incomplete operations, per-window load, and the same metric definitions. Separate free-memory wait, transfer-slot wait, worker queue and actual API/helper intervals. No percentile subtraction or summation.
8. A successful repair should remove the long placement tail during concurrent migration without changing placement/accounting; reduce true migration or startup service rather than move it into a hidden queue; and improve successful tool/session completion at comparable load. Quantitative targets should be set from measured phase costs, not the advertised single-VM200 ms figure. Finish with full-denominator throughput/E2E if the run completes; otherwise preserve diagnostic evidence and say what remains unproven.

Use saved machine settings and manifest values with the CLI syntax in the original handoff. Inspect current help and describe the prepared spec before launching. Do not invent machine/env/template identities or use the historical remote Git base as deployed-source proof. No launch was attempted or validated during this review because SSH was unavailable.

The original plan's eight final-report questions remain relevant. This review supplies service-overhead attribution and next actions; it does not replace missing complete Full runs, exact scheduled-origin E2E, successful-session throughput, or a fresh zero-owned-VM cleanup audit. The earlier five-hour deadline has passed; do not restart that clock or claim the original goal was met.
