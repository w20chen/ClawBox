# Full service-overhead diagnosis handoff

Prepared 2026-10-27 Asia/Shanghai for an independent Codex session. This is the
current diagnosis and execution plan. It supersedes the priority ordering in
`FULL_SERVICE_OVERHEAD_HANDOFF_20261026.md`; that file and
`FULL_LATENCY_HANDOFF_20261025.md` remain useful for the earlier evidence and
historical fixes.

## 1. Objective and decision rule

Reduce time beyond real guest Tool execution and recorded LLM wait at Full-1280
and Full-2560. Tens of milliseconds are preferred, about 100 ms is acceptable,
and a recurring non-memory interval around or above one second is a critical
investigation target. Waiting for genuinely unavailable memory can be reported
as a capacity wait. CPU scheduling, GIL/thread contention, lock contention,
serial `fsync`, unnecessary scans/hashes, synchronous logging, avoidable
checkpoint waves and inefficient Cube/API use are optimization defects.

Use event occurrence timestamps and per-stage durations. Do not add stage
percentiles, subtract unrelated percentiles, or infer a complete-run E2E result
from the cancelled diagnostic below. Small tests are correctness gates. Any
consequential speed claim needs comparable 1280 and 2560 evidence. Commit and
push each verified milestone, but combine minor fixes into the next material
large run instead of repeatedly rebuilding or replaying the paper workload.

Supported scope is the standalone `clawbox experiment` path, CubeSandbox and
ClawTune integration. Do not create a second experiment path or extend the
deprecated control plane. Preserve the measured replay waits, prediction
semantics, incremental snapshot policy, placement policy and per-session
writable state unless the user explicitly approves a semantic change.

## 2. Primary current evidence and its limits

The latest high-load diagnostic is:

- Run: `full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst`
- Attempt: `attempt-ae5551d386034434`
- Arm: `a0e2c177dd4693ea054f15ad`
- Host/native deployment: ClawBox commit `6276057`
- Offered sessions: 2,560 over the frozen 741.1812062263489-second Poisson
  window, about 3.45 session pairs/s
- Outcome: intentionally cancelled after the diagnosis became clear; 25
  validated sessions, 53 observed final responses, 28 failed sessions, 525
  agents runnable, 532 pairs ready and 1,064 sandboxes created
- Cleanup: recorded as verified by the deployment artifact

Remote event stream:

```text
/data/clawbox-service-overhead-20261026T1715CST/v6/runs/
  full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst/
  attempts/attempt-ae5551d386034434/a0e2c177dd4693ea054f15ad/
  events/a0e2c177dd4693ea054f15ad.jsonl
```

Derived analysis:

```text
/data/clawbox-service-overhead-20261026T1715CST/v6/analysis/
  full-c2560-6276057-at-2560-offered.json
```

Reproduce it on `ssh kunpeng` with:

```bash
python3 /data/clawbox-service-overhead-20261026T1715CST/v6/analyze_overhead.py \
  /data/clawbox-service-overhead-20261026T1715CST/v6/runs/full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst/attempts/attempt-ae5551d386034434/a0e2c177dd4693ea054f15ad/events/a0e2c177dd4693ea054f15ad.jsonl
```

This run is diagnostic, not a complete Full-2560 result. Its service
distributions contain only completed operations. The create-gate distribution
is especially censored: all 2,560 sessions started waiting, but only 547 had
acquired the gate when the run stopped. The observed gate p50 therefore
understates the median across all offered sessions.

Frozen material configuration:

- 160 experimental CPUs, CPU set `0-159`
- two 500-GiB LOCAL domains; 1,000 GiB aggregate capacity
- LOCAL low watermark 917,504 MiB and high watermark 983,040 MiB
- 100 GiB shared-live limit and 100 GiB WARM snapshot capacity on NUMA node 2
- Tool VM 2 vCPU/3 GiB; Runtime VM 2 vCPU/1 GiB
- create gate 16 session pairs, hence up to 32 simultaneous Cube creates
- total lifecycle transfer concurrency 32; checkpoint concurrency 24
- incremental-CoW, tiered WARM/COLD snapshots
- peak sampled experiment cgroup CPU 121.34 of 160 cores
- peak sampled LOCAL/cgroup memory about 879 GiB

The host did not reach the analysis threshold of 140 CPU cores. Memory pressure
was nevertheless triggered from projected demand/reservations: up to 16
in-flight 4-GiB pairs add about 64 GiB of protected headroom. Do not classify
all early overhead as memory pressure: most of the run's Tool calls and 958 of
1,064 creates preceded the first pressure event.

## 3. Current critical overhead ranking

All values are seconds and are observed completed-operation distributions from
the cancelled Full-2560 trace unless noted otherwise.

| Path | n | p50 | p90 | Current judgement |
|---|---:|---:|---:|---|
| Dispatch to create-gate acquisition | 547 | 58.895 | 435.621 | Severely censored backlog; create throughput is below arrivals |
| Create gate acquired to pair ready | 532 | 15.902 | 53.738 | Critical VM activation service |
| Per-VM `api_create_s` wrapper | 1,064 | 12.127 | 39.882 | Not pure Cube time; includes ClawBox journal `fsync` |
| Checkpoint total | 220 | 35.527 | 63.684 | Mostly a 24-slot burst queue |
| Checkpoint transfer-slot wait | 220 | 28.616 | 57.065 | 220 operations arrive in roughly nine waves |
| Checkpoint API wrapper | 220 | 6.673 | 9.157 | Still materially slow service |
| Restore total | 70 | 49.450 | 94.983 | Queue plus very slow restore service |
| Restore transfer-slot wait | 70 | 25.316 | 40.456 | Three or more transfer waves |
| Restore API wrapper | 70 | 31.250 | 50.491 | Critical native/SDK/retry service |
| NUMA live borrow | 2 | 101.833 | 106.210 | Very small sample, but both are unacceptable |
| Agent Tool non-guest time | 27,625 | 2.610 | 6.767 | Dominates the median 85-ms guest command |
| Agent Tool queue to admission | 27,685 | 0.806 | 2.966 | Mostly controller/scheduling, not explicit memory wait at p50 |
| Agent Tool guest end to complete | 27,625 | 1.019 | 3.285 | Completion callback/sampling/release path is too slow |
| Pair ready to SSH warm | 525 | 2.920 | 4.165 | Actual SSH/startup work; warmup-pool queue is small |
| SSH warm to agent runnable | 525 | 1.752 | 2.344 | Runtime setup/sidecar readiness remains above target |
| Destroy | 104 | 5.171 | 7.698 | Usually after final response, but delays capacity release |

Agent Tool guest execution itself was p50 0.085 and p90 0.836. Explicit
memory-admission blocking was p50 0.0033 and p90 0.293. The p50 2.610 seconds
of non-guest time is therefore not explained by genuine memory waiting.

## 4. Critical issue A: VM creation cannot keep up

### What is proven

Sixteen pair slots with p50 15.902-second pair service provide approximately
`16 / 15.902 = 1.0` pair/s at the median. The workload offers about 3.45
pairs/s. The queue must grow. At the current gate, pair service must fall below
about `16 / 3.45 = 4.64` seconds merely to match arrivals.

Raising the gate is not the first fix. Gate 16 already generates up to 32
simultaneous Cube creates because Tool and Runtime are created concurrently in
`start_sandbox_pair()`. Service deteriorated under load. An earlier, lighter
v5 interval observed create API p50 0.262 seconds, whereas the latest high-load
trace observed 12.127 seconds. This is a diagnostic contrast, not a controlled
speedup/regression comparison, but it proves the path is load-sensitive.

The create-stage decomposition in the latest trace was:

- total create p50 12.644
- `api_create_s` p50 12.127
- transfer-slot wait p50 9.6 microseconds
- cgroup preparation p50 1.16 ms
- cgroup verification p50 87.25 ms
- NUMA pin p50 5.53 ms
- placement lock wait p50 3.35 microseconds

Before first pressure, `api_create_s` p50 was about 10.917 seconds; after
pressure it was about 38.700 seconds. Tool and Runtime were close:

- Tool: all 12.348, before pressure 11.272, after pressure 42.440 p50
- Runtime: all 11.859, before pressure 9.977, after pressure 38.339 p50

The refreshed Tool images therefore are not the create bottleneck. Pressure
amplifies the problem, but an avoidable roughly 11-second path exists before
pressure.

### Verified measurement defect and likely implementation bottleneck

`clawbox/cube/lifecycle.py` records `api_create_s` around the entire
`CubeSandboxClient.create_sandbox()` call, not just the official SDK request.
After `Sandbox.create()` returns, `clawbox/cube/client.py:218-221` inserts the
handle and calls `OwnedSandboxJournal.record()`.

`OwnedSandboxJournal.record()` at `clawbox/cube/client.py:49-56` takes one
process-wide lock, opens the file, writes, flushes and calls `os.fsync()` while
holding that lock. With up to 32 simultaneous creates, ownership records and
their durable disk flushes serialize inside the interval currently labelled
Cube API time. Snapshot I/O can make those `fsync`s much worse.

The contamination and serialization are proven in source. Their exact share
of the 12.127-second p50 is not yet measured. Do not state that Cube itself is
12 seconds or that the journal is the entire 12 seconds.

Our parallel API pattern is not inherently wrong. Cube's official concurrent
create example also invokes `Sandbox.create()` from a thread pool. Cube's
published benchmark reports subsecond creates at concurrency up to 50 on a
different x86/NVMe host with smaller 2-GiB VMs. Those absolute numbers are not
a baseline for this ARM host, but they show that 10-40 seconds is not an
intrinsic API requirement:

- https://github.com/TencentCloud/CubeSandbox/blob/master/examples/snapshot-rollback-clone/bench_create_concurrency.py
- https://github.com/TencentCloud/CubeSandbox/blob/master/docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md

### Required repair and proof

1. Split `sdk_create_s`, `journal_lock_wait_s`, `journal_write_s` and
   `journal_fsync_s`. Keep the existing outer total.
2. Run the official direct create pattern and the ClawBox wrapper side by side
   on this host/template at concurrency 1, 10, 20, 32 and 50. Record throughput
   as well as p50/p90. Do not launch another Full run to answer this split.
3. Replace one-`fsync`-per-create with a durable group-commit writer: queue
   records, append all currently pending records under one writer, issue one
   `fsync`, then acknowledge every record in that batch. A persistent file
   descriptor is sufficient. Do not remove durability: cleanup after a worker
   crash relies on this journal.
4. Re-run the direct/wrapper sweep and choose the create gate at the throughput
   knee. Do not assume 16 or 50 is optimal.
5. If the SDK/request remains material after removing journal time, test Cube's
   official direct asynchronous HTTP approach inside `CubeSandboxClient`, not
   as a second experiment workflow:
   https://github.com/TencentCloud/CubeSandbox/blob/master/CubeAPI/examples/benchmark.py
6. Only then validate the combined result at 1280 and 2560. If sustainable
   capacity remains below 3.45 pairs/s, report it honestly and lengthen the
   offered-arrival window as a capacity-matched experiment; that is not a
   latency optimization.

Do not pre-create a generic pool without a design review. Session-specific
environment variables, credentials, owner metadata and cgroup placement are
currently applied at creation. Pooling would change semantics unless bootstrap
injection is redesigned.

## 5. Critical issue B: excessive Python threads and GIL/OS scheduling

### What is proven

`session_worker_count()` returns the arm concurrency for a normal run, and
`worker.py:2426-2428` creates a `ThreadPoolExecutor(max_workers=2560)`. All
session tasks are submitted; workers sleep until their Poisson offer and then
block on gates. Each pair also creates a fresh two-thread executor at
`worker.py:97-110`. The warmup pool adds up to 64 threads.

Every admitted Tool command constructs `SandboxRSSSampler`. It creates a daemon
thread, wakes every 50 ms, and reads `memory.current` plus per-process RSS/fault
counters (`memory.py:546-645`). Completion calls `host_sampler.stop()`, joins
that thread, takes final samples and releases memory/CPU while holding the
session's `reservation_lock` (`worker.py:4129-4179`).

At high load, normally trivial Python stages show random wall-time inflation:

- prediction/queue p50 59 microseconds, but max 4.426 seconds
- bookkeeping p50 17 microseconds, but max 4.453 seconds
- lifecycle-lock p50 below one microsecond, but max 9.791 seconds
- memory baseline p50 1.32 ms, but max 2.428 seconds
- sampler start p50 5.56 ms, but max 3.488 seconds

The machine still had global CPU headroom. This pattern is consistent with GIL
and OS descheduling among thousands of threads, not with those individual
operations performing seconds of useful work. Stacks captured during earlier
pressure also show thousands of `agent_*` threads; see
`.runtime/service-overhead-20261026T1715CST/analysis/worker-stacks-v2-pressure.txt`.

The exact composition of guest-end-to-complete p50 1.019 seconds remains
unmeasured. The likely contributors are callback scheduling, acquiring
`reservation_lock`, joining the per-command sampler, final `/proc`/cgroup
reads, coordinator/CPU release and event emission. Treat this as a hypothesis
until those sub-stages are timestamped.

### Required repair and proof

1. Replace one sleeping worker per offered session with one arrival scheduler
   or priority queue. At each due offset, enqueue the session into bounded
   create/active work; do not materialize 2,560 blocked Python workers.
2. Bound the active-session executor by real runnable/resident capacity. Keep
   the same frozen offer offsets and record dispatch lateness so scheduler lag
   is not hidden.
3. Replace one `SandboxRSSSampler` thread per command with one shared sampler
   service that registers active sandbox/execution IDs, batches 50-ms reads and
   unregisters them on completion. Preserve per-execution baseline, peak,
   final, fault and validity fields.
4. Move sampler stop/final sampling outside `reservation_lock` where safety
   permits. Keep reservation publication/removal atomic.
5. Add timestamps for callback receipt, reservation-lock wait/hold, sampler
   stop/join, final sample, coordinator release, CPU release, activity update
   and event write.
6. Prefer async/nonblocking I/O for Cube HTTP, policy calls and similar control
   waits. Use a small bounded thread pool only for blocking SDK operations.
   Process pools are appropriate only for demonstrated CPU-bound Python work;
   most of this path is I/O and scheduling.
7. The current 1-ms Python thread switch interval is only a mitigation. Do not
   claim it solves this architecture.

Acceptance is not just lower CPU. Under comparable Full-1280 and Full-2560
load, non-memory Tool queue-to-admit and guest-end-to-complete should approach
the sub-100-ms goal without losing measurement records or changing guest Tool
execution.

## 6. Critical issue C: checkpoint/restore burst queues and slow service

### Queueing is exactly attributable

The snapshot transfer gate is 24. A pressure decision produced 220 completed
checkpoints, roughly nine waves. Checkpoint API p50 is 6.673 seconds; a median
request waiting roughly four earlier waves predicts about 26.7 seconds, close
to the measured 28.616-second transfer wait. This is a bounded resource queue,
not a mutex bug, but the policy's burst turns expected serialization into an
end-to-end cliff.

Seventy restores require at least three waves, and the restore operation itself
has p50 31.250 seconds. Later waves therefore wait tens of seconds. Increasing
24 blindly may reduce queue time but worsen every operation through storage and
memory-bandwidth contention.

Checkpointing starts from projected demand, not only instantaneous RSS. The
sampled peak of about 879 GiB is below the 917-GiB low watermark, but in-flight
pair reservations can add about 64 GiB. This capacity protection is reasonable.
The 220-operation burst and slow service are the optimization targets.

### Service path and remaining unknowns

Checkpoint uses `pause(wait=True)` plus the custom direct tiered
incremental-CoW path, snapshot manifest validation and metadata/lineage reads
(`client.py:600-648`, `lifecycle.py:398-510`). Restore calls official
`Sandbox.connect()` with `snapshot_mechanism=incremental-cow` through ten
attempts of 0.25-to-2-second retry/backoff (`client.py:224-241`). The current
event does not expose retry count or sleep, so `api_restore_s` is not pure
hypervisor work.

Plausible checkpoint service components are pause/device state, dirty-page
scanning/writing, WARM tmpfs or COLD XFS bandwidth, metadata/lineage work and
contention among 24 writers. Plausible restore components are Cube scheduling,
shim/hypervisor state restoration, external-base/private-delta mapping, page
tables/faults and hidden retry/backoff. These components are not yet separately
measured.

Cube's official simple benchmark reports much faster pause/resume on a
different host and workload, so 31-second restore should not be accepted as an
API invariant. Official lifecycle semantics still use pause and connect; the
basic API choice is correct:

- https://github.com/tencentcloud/cubesandbox/blob/master/sdk/python/README.md
- https://github.com/TencentCloud/CubeSandbox/blob/master/docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md

### Required repair and proof

1. Log each restore attempt, response/error, retry sleep and successful Cube
   call separately. Log checkpoint dirty/logical/allocated/transferred bytes,
   storage tier, generation and lineage depth. The current API sometimes cannot
   expose transferred bytes; state that explicitly instead of inventing it.
2. Correlate operation IDs with CubeMaster, Cubelet and CubeShim timestamps:
   request receipt, scheduling, pause/restore begin, memory/device work and
   response.
3. Run a targeted transfer sweep at 8, 16, 24 and 32 using representative
   3-GiB Tool snapshots. Measure aggregate operations/s, per-operation latency,
   memory bandwidth and storage bandwidth. Select the throughput knee.
4. Stagger/coalesce pressure checkpoint dispatch. Start reclaim earlier and
   maintain headroom rather than releasing about 220 operations in one wave.
   Prioritize low-value/oldest-idle VMs and avoid checkpointing a VM likely to
   execute again immediately.
5. Keep hot snapshots in WARM when beneficial; use COLD for colder state.
   Compact/rebase long incremental lineages only if measured restore time
   correlates with lineage depth.
6. Compare the custom incremental-CoW path with the installed Cube native
   full-copy pause/resume on the same host and VM size as a diagnostic. Do not
   silently change the frozen experiment mechanism; qualifying another
   mechanism creates a new baseline.

## 7. Critical issue D: NUMA borrow spends tens of seconds in cpuset work

The latest run completed only two live borrows, so no population percentile is
valid. Both are nevertheless severe:

- borrow 1: total 96.361; transfer wait 10 microseconds; native prepare 1.293;
  pre-migrate 4.226 with two pages remaining; **cpuset phase 90.678**; resume
  5.4 ms
- borrow 2: total 107.304; transfer wait 35.825; native prepare 3.005;
  pre-migrate 0.457 with zero pages remaining; **cpuset phase 67.938**; resume
  5.6 ms

The slow phase is now the cgroup cpuset transition, not bulk page migration.
`numa_rebind.py:136-174` creates threaded source/target children, attaches the
KVM process, changes the parent memory mask, attaches to the target, restores
affinity, attaches back and removes the children. The existing timer covers all
of those operations as one `cpuset_seconds` interval. Even when pre-migration
leaves zero or two pages, the kernel cpuset path can flush global migration
work or wait under cgroup/cpuset serialization.

An earlier source bug held a borrower-domain lock across slow migration and
blocked unrelated restore placement checks. That was changed to per-VM locks.
The latest restore `pin_local` lock-wait p50 is 2.8 microseconds and p99 below
four microseconds; do not reopen the old global-lock design. The remaining
problem is the kernel/cgroup operation itself and the one borrow that also
waited 35.8 seconds for a transfer slot.

Required next work:

1. Time every `_write`/attach in `numa_rebind.py`: source creation/config,
   source attach, parent mask expansion, target creation/config, target attach,
   affinity restore, parent final mask, return attach, child removal and
   subtree disable.
2. Capture short kernel stacks/perf only while a slow cpuset write is blocked.
   Determine whether the wait is page migration, ordered workqueue flush,
   cgroup attach serialization, reclaim or another kernel wait.
3. Check current official CubeSandbox placement/cgroup APIs and Linux cpuset
   semantics before preserving the custom threaded-child dance. A replacement
   must keep existing-page migration, future-allocation policy, KVM pause/RAM
   detach, dirty-bit correctness, CPU affinity and residual shared-memory
   accounting. Do not optimize by weakening those semantics.
4. Test unrelated VM progress while VM A is blocked, same-VM serialization,
   failure after prepare and reservation cleanup. Then validate actual borrow
   latency under 1280/2560 pressure.

## 8. Remaining startup, completion and cleanup overhead

### Startup after pair readiness

The old 2026-10-26 diagnostic saw sidecar readiness p50 25.739 seconds. Runtime
predictor caching, startup-gate parallelism, reusable SSH connections, image
preparation and OpenClaw skill-refresh changes substantially reduced the newer
trace, but current startup still exceeds the target:

- Runtime setup stage p50 about 1.082 seconds
- sidecar-ready stage p50 about 2.332 seconds
- OpenClaw configuration p50 about 43 ms
- native SSH warmup worker queue p50 about 1 ms and p90 about 7 ms
- native SSH warmup execution p50 about 0.643 and p90 about 1.205 seconds

The shared 64-worker warmup queue is no longer the main cause. Actual Runtime
setup, sidecar readiness and transport work remain. Preserve strict sandbox
identity/host-key verification. If optimizing next, timestamp RPC send/return,
guest process launch, listener-ready and health success; reuse established
connections rather than weakening identity checks.

### Completion

Guest-end-to-complete p50 1.019 seconds is critical because it occurs after
real Tool work and delays result delivery and reservation reuse. Section 5
describes the likely sampler/join and scheduling causes. Instrument it before
changing release ordering.

### Destroy

Destroy p50 5.171 and p90 7.698 generally occurs after the final-response E2E
endpoint, but it delays VM/cgroup/memory release and therefore worsens later
sessions. Split Cube delete, physical disappearance observation, cgroup cleanup
and journal/ledger cleanup. Optimize it after create, thread/sampler and
snapshot work unless evidence shows it is blocking capacity sooner.

### Secondary tails

- create transfer-slot wait is microseconds through p90 but p99 27.910 seconds;
  snapshot waves can consume the shared 32-operation lifecycle gate
- create cgroup verification p99 1.631 seconds
- restore cgroup verification p50 0.249, p90 1.118 seconds
- per-session lifecycle lock is normally sub-microsecond but has a 9.791-second
  max; log its holder before calling it a lock bug because same-VM lifecycle
  serialization can be required
- 28 observed session failures include unexpected replay Tool failures and
  incomplete gateway sessions; preserve them in denominators and do not report
  a performance win by dropping failed work

## 9. Confirmed fixes and findings not to repeat

These are already implemented or ruled out as current primary bottlenecks.
Preserve them unless a new trace disproves them.

| Item | Current status/evidence |
|---|---|
| Tool BCC/Clang compilation on first command | Moved into a resident prewarmed collector captured by each Tool template. Old first command 6.582 seconds; refreshed-template trials 0.438 and 0.387 seconds, mean about 0.413. The saving is about 6.17 seconds. |
| 503 Tool templates | `/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/collector-update-state.json` has 503 records, all `template-ready`, with 503 template IDs. Workload/catalog point at them. Do not rebuild them again unless the collector or Tool image content changes. |
| Template validity | Pilot checked first/second command, collector health, preserved collector PID across bridge restart and cross-VM private-state isolation. The refreshed catalog also passed 503-case qualification at concurrency 32. This is correctness evidence; the latest Full run is the high-load usage evidence. |
| Runtime reusable state | Predictor cache and seed cache are built/exposed in the Runtime image; unusable Runtime eBPF startup is skipped. Preserve commits `2ac74d0`, `ff74a29`, `e4410e3`. |
| Repeated image inventory/startup gates | One Cube inventory validates templates and immutable gates run in parallel. Preserve `6a04bc6`, `3e57117`. |
| SSH connection setup | OpenSSH connections are reused within endpoint epochs; OpenClaw SSH skills refresh once per turn rather than every command. Preserve `f3809c4`, `fb9ac8d`. |
| Admission global scans/locks | High-concurrency scans, duplicate active locking and baseline publication under the global lock were removed; low-pressure activation/admission was fused. Preserve `17a62ea`, `a23d61b`, `b7eeddc`, `213af8b`, `089b9d3`. |
| Memory baseline locks | Latest 35,245 observations show zero measured lookup/publish lock wait; baseline service p50 1.32 ms, p90 9.74 ms. Not the current median bottleneck. |
| Event writer | It now keeps a buffered handle and flushes every 128 events or 250 ms. Lock wait p50 0.63 microseconds, p90 0.93 microseconds, p99 2.84 ms across 258,350 events. Do not prioritize another logging rewrite without CPU/I/O evidence. |
| Typical prediction/bookkeeping | Prediction p50 59 microseconds and bookkeeping p50 17 microseconds. Their multi-second maxima are evidence of global scheduling delay, not expensive prediction logic. |
| NUMA placement lock | Per-VM lock wait is microseconds in the latest create/restore trace. The old borrower-wide lock problem is not the current restore bottleneck. |
| CubeMaster debug volume | Host apply now persists the correct uppercase log level. Preserve `9d7ae60`, `581e6e4`, `4f285a9`. |
| Create capacity rejection | Definite Cube 130597 rejections retry outside the shared lifecycle slot; ambiguous POST timeouts reconcile ownership rather than duplicate creates. Preserve `c2c5024`. |
| Initial/reused cgroup placement | Current native patches assign cpuset memory before publishing reused leaves and preserve CPU affinity without redundant memory-policy syscalls. Preserve `bda9eb6`, `aee3f6a`, `6276057`; these do not solve later live-borrow cpuset transitions. |

The prewarmed collector is ClawTune code packaged into the Tool image/template.
A ClawTune change requires refreshing the affected templates only when it
changes that packaged collector or its dependencies. Runtime-only or host-only
ClawTune changes do not imply rebuilding all 503 Tool templates. Cube templates
are pre-warmed microVM snapshots, so per-template process state is captured;
identical underlying image blocks can be shared/reflinked, but the 503 template
identities remain distinct.

Official template guidance:
https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/templates.md

Refresh/validation evidence is at:

```text
/data/clawbox-service-overhead-20261026T1715CST/v4/prewarm-probe.json
/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/collector-update-state.json
/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/catalog.json
/data/clawbox-service-overhead-20261026T1715CST/v5/template-sharing.json
/data/clawbox-service-overhead-20261026T1715CST/v6/qualification-cpuset-memory.json
```

## 10. Recommended milestone order

### Milestone 1: make create time truthful and remove serial journal flushes

- Add the four create/journal stage timers.
- Run the small direct-versus-wrapper concurrency sweep.
- Implement durable journal group commit and targeted crash/cleanup tests.
- Re-run the sweep and select the create gate from throughput.
- Commit and push with the diagnostic artifact path and exact before/after
  service/throughput numbers.

This is first because VM backlog dominates startup and because the current
measurement incorrectly attributes ClawBox work to Cube.

### Milestone 2: remove thread explosion and per-command sampler threads

- Implement the event-driven arrival scheduler and bounded active work pool.
- Implement one shared sampling service.
- Add completion sub-stage timestamps and preserve all measurement fields.
- Use targeted concurrency tests, then one Full-1280 and one Full-2560 run for
  the combined scheduler/sampler change.
- Commit and push the verified result.

### Milestone 3: reshape and profile checkpoint/restore

- Add retry, bytes, tier, generation, lineage and native server timestamps.
- Sweep transfer concurrency with representative snapshots.
- Stagger/coalesce pressure actions and select the measured concurrency knee.
- Validate checkpoint integrity, restore state, cleanup and Full-scale latency.
- Commit and push.

### Milestone 4: replace or repair the live-borrow cpuset transition

- Split every cgroup operation and capture blocked kernel stacks.
- Prefer an official Cube/Linux mechanism if it preserves the same placement,
  migration and accounting semantics.
- Verify same-VM safety and unrelated-VM progress, then validate at pressure.
- Commit and push.

### Milestone 5: remaining startup, destroy and tails

- Optimize the measured sidecar/SSH phase and destroy service only after the
  larger items above.
- Combine small tail fixes into the next consequential run.

Do not start by rebuilding images, changing Cube versions, raising all
concurrency limits or rerunning the full baseline. The 503 refresh is complete.
A Cube upgrade is a new baseline and requires separate qualification.

## 11. Acceptance and reporting

For every important fix, preserve:

1. exact source commit and pushed branch;
2. frozen YAML, workload/catalog hashes and offer offsets;
3. unique run/attempt/arm IDs and complete event JSONL;
4. host/native deployment identity;
5. operation counts plus failed/cancelled/incomplete counts;
6. p50/p90/p99 service and queue distributions with definitions;
7. CPU, memory and storage pressure during the measured window;
8. cleanup verification and owned-sandbox ledger.

Report scheduled-arrival-to-final-response E2E separately from validation and
cleanup. Compute LLM and Tool wall-time coverage as interval unions; concurrent
Tool durations cannot be summed and subtracted from E2E. Separate genuine
memory admission waits, transfer-slot waits, create-gate waits and actual
service. If a run is cancelled, report the outstanding queue ages and the
denominator censoring.

The end condition is not merely passing tests. The supported workflow must
retain correct results and metadata, while the largest non-memory critical-path
overhead is reduced as far as the time budget reasonably allows at both 1280
and 2560. If a component remains above one second after a measured best effort,
document its demonstrated hardware/native limit and move to the next-largest
critical-path source rather than spending hours on speculation.

## 12. Current workspace and remote cautions

- Local workspace at handoff creation: `C:/Users/29068/Desktop/ClawBox`, branch
  `main`, starting HEAD `6276057`, clean before this new handoff.
- Main remote study: `/data/clawbox-service-overhead-20261026T1715CST`.
- The latest run's outer state is terminal/cancelled and cleanup is verified,
  although its nested arm record can still say `running`; refresh API/process
  inventory before assuming anything remains active.
- Preserve unrelated paused sandboxes, `anns`, old template storage and other
  workloads. Clean only IDs proven to belong to the selected run ledger.
- `.runtime` is ignored and contains local diagnostic copies. A different
  machine needs the remote `/data` artifacts or an explicit evidence copy; Git
  alone contains the handoff and source, not the large event stream.
- Follow `AGENTS.md`: use maintained scripts/CLI, do not reinstall the shared
  host for a test, keep public configuration documented, and distinguish unit,
  qualification, real-VM and Full-scale evidence.
