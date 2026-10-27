# VM creation: cgroup placement repair and next bottleneck

The cgroup placement repair is implemented and pushed in `b5943df`. It removes
the measured multi-second initial attachment delay while retaining the selected
NUMA node, Cube's existing cgroup pool, and the previous snapshot/IRQ optimizations.
Total VM creation is still above one second for many requests. The next measured
bottleneck is virtual-device restoration and kernel IRQ-bypass registration.

## Change and correctness

`deploy/cubesandbox/cgroup-birth.patch` starts the single-threaded shim launcher
in its final cgroup with `clone3(CLONE_INTO_CGROUP)`, then re-executes the ordinary
containerd launcher. Its child inherits placement. The shim verifies membership
before guest RAM allocation, and Cubelet skips an already-satisfied assignment.
This avoids both moving existing shared mappings and flushing the shared cpuset
migration queue through redundant `cgroup.procs` writes.

- Native CubeShim release build and targeted Cubelet membership tests/build passed.
- Real VMs on NUMA nodes 0 and 1 passed creation, two incremental checkpoint/restore
  cycles, two shared-memory borrow/return cycles, guest-data integrity, thread CPU
  affinity, and cleanup. Both checks ended with parent `populated 0`.
- Fresh source preparation, repeated preparation, and rejection of a mismatched
  patch-set marker passed on `kunpeng`. All six changed native source files match
  the prepared source. Thirteen Linux installation/CLI tests passed.
- Two malformed pre-existing patch hunk counts were corrected so fresh source
  preparation works; their actual code changes were not altered.
- No clean-machine installation or full workload-completion claim is made.

The first candidate printed a success message into containerd's launcher-address
protocol and failed the real-VM check. That output was removed before the successful
checks and performance runs. The deployed shim SHA256 is
`4232daf0d544f2454303aa338383c7618ea44ad8658fdf3431844f4d253cab7b`;
Cubelet is `f2e4f29d79ac2e7e29351b6bbf382b6de42194a6fc04410bd44b9f6ebbcc236ec`.
Both originals and the rejected first candidate are retained in the evidence root.

## 1280 comparison

Run `full-c1280-cgroup-birth-20261027t163755cst`, attempt
`attempt-0f1fe92940c14de3`, used the ordinary `clawbox experiment launch` path.
The workload, templates, 741.1812062263489-second Poisson window, seed 20261025,
160 experiment CPUs, two 500-GiB LOCAL domains, shared/WARM budgets, create limit
16 pairs, transfer limits, and incremental-CoW policy were unchanged.
The existing capability receipt was reused and the new native checks recorded
separately. Python instrumentation remained commit `5c3b0669f5910dd2e42717bb34efbe7a3204fe59`.

All values below are seconds. Distributions include completed creations only.

| Interval | Baseline median / p90 / p99 / max | Candidate median / p90 / p99 / max |
|---|---|---|
| SDK create | 4.817 / 27.108 / 56.361 / 76.804 | 0.459 / 2.292 / 7.385 / 9.768 |
| Spec loaded to placement verified | 0.857 / 21.665 / 49.505 / 69.834 | 0.000255 / 0.000333 / 0.000929 / 0.310 |
| VM start to ready | 0.582 / 4.257 / 6.832 / 8.436 | 0.345 / 1.844 / 6.336 / 9.570 |

Baseline: 1,164 creates, 582 pairs ready, 53 sessions complete, 127 failed in its
final diagnostic snapshot. Candidate pre-abort: 1,306 creates, 653 pairs ready
and runnable, 17 sessions complete, 22 failed. Both offered 1,280 sessions.
Candidate failures comprise 16 Cube API resource rejections (130597), four
unexpected replay Tool failures, and two incomplete ModelGateway sessions.
The candidate has 356/1,306 SDK calls above one second, but zero placement
intervals above one second. This is a large creation-service improvement, not
proof that every session succeeds or that the entire creation path meets 1 s.

The diagnostic was stopped after all arrivals and a 184.9-second tail observation.
At cutoff, 595 sessions still waited for the create gate and 16 held it; their
ages are preserved in `pre-abort-state.json`. Pending requests are censored, not
assigned zero latency. The distributions have different populations and observation
lengths; this is not a matched full-workload throughput or E2E comparison.
Configured admission budgets must not be confused with host memory pressure:
the captured parent cgroup reported no OOM/max events and host memory PSI was zero.

The first launch, `full-c1280-cgroup-birth-20261027t163512cst`, failed before VM
creation because the policy-control port was in use. It remains a separate run.
The successful diagnostic was cancelled through the maintained CLI, cleanup
returned success, and parent `populated 0` was verified before the 2560 launch.

## 2560 validation

Run `full-c2560-cgroup-birth-20261027t165547cst`, attempt
`attempt-9e799404a8184d4a`, uses the same binaries and settings, with offered
concurrency increased to 2560. Its configuration matches the preceding v6
2560 configuration apart from experiment ID. At cutoff: 2,560 sessions offered,
1,466 VMs created, 733 pairs ready/runnable, 10 sessions complete, 36 failed.
Failures comprise 32 Cube API resource rejections (130597), two unexpected replay
Tool failures, and two incomplete ModelGateway sessions. These unresolved failures
prevent a full-workload correctness claim; native resource rejections are not
automatically evidence of actual host memory exhaustion.

| Interval | Median | p90 | p99 | Maximum |
|---|---:|---:|---:|---:|
| SDK create | 2.782 | 8.250 | 11.317 | 16.328 |
| Spec loaded to placement verified | 0.000290 | 0.000380 | 0.008498 | 0.105 |
| VM start to ready | 2.418 | 7.461 | 9.688 | 13.618 |

All intervals are seconds; all 1,466 completed creations have matching shim
markers. Placement stayed below one second in every observation. SDK creation
exceeded one second in 906/1,466 observations, with VM start-to-ready exceeding
one second in 890/1,466. This validates removal of the placement bottleneck at
the higher offered load; it does not establish subsecond overall VM creation.

The tail observation lasted 192.3 seconds after the final arrival. At cutoff,
1,779 sessions waited for the create gate and 16 held it. The run was cancelled
through the maintained CLI; cleanup succeeded. Final verification found parent
`populated 0`, no remaining owned tracing instances, and all diagnostic capture
processes finished. `cleanup-final.json` records both runs' cleanup receipts.

## Further reduction: device restoration and IRQ bypass

Three slow 1280 creations isolated the residual delay:

| VM | SDK create | Native RestoreVm | Device restoration | Interrupt controller |
|---|---:|---:|---:|---:|
| `5c4f2bcc7ebc4e7dbb0900a6927cca7f` | 9.768 | 6.001 | 5.944 | 0.051 |
| `97031c0846e24d2ea9a9d5ccaed43c24` | 9.598 | 9.462 | 9.045 | 0.405 |
| `0c2cb38dd3df4802b9e279626e600413` | 9.678 | 9.523 | 8.598 | 0.911 |

The first also spent 3.607 seconds in readiness probing. These are nested
intervals; do not add SDK, RestoreVm, and its substages together.

A 45-second isolated kernel trace during the 2560 run captured 2,100 matched
IRQ registrations. `irq_bypass_register_consumer` took median 0.173 s, p90
0.387 s, p99 0.612 s, max 2.266 s. `kvm_irqfd_assign` had almost identical
durations. Of 1,567 unregister operations, 612 exceeded one second; p99 was
2.392 s, max 2.492 s. There were zero trace-buffer overruns. Sampled blocked
threads were inside `irq_bypass_register_consumer -> kvm_irqfd_assign -> ioctl`.
These are mixed lifecycle operations during the observed window, not a per-VM
sum or a full-run percentile distribution.

Joining sampled thread ownership to SDK windows attributes 10.529 seconds of
VM `6eca14f50168424bb1476c01d9ef9aef`'s 15.895-second creation to 30 registration
calls. VM `dfa83b552bf049f5b0f7444c869371f6` spent 8.632 of 10.057 seconds there.
These are unions of measured intervals, not sums of overlapping calls. The
correlation artifact records partial trace-window and sampled-thread coverage.

The Linux 6.6 implementation uses host-wide linked lists protected by one mutex.
Registration searches the consumer list and producer list; teardown also searches
the lists. Costs grow with the host's registered interrupts, and concurrent VM
creation/restoration/teardown contend for that same lock. The current host is
`6.6.0-72.0.0.76.oe2403sp1.aarch64`; KVM and IRQ bypass are built in.
Its installed matching-version `irqbypass.h` also confirms the linked-list layout.

Upstream Linux 6.18 replaces these scans with XArray lookup and stores direct
producer/consumer associations. It retains a shared mutex, so improved lookup
does not guarantee all contention disappears. Prefer evaluating a supported
kernel containing that upstream redesign, or a carefully scoped backport, before
inventing a different interrupt-delivery mechanism. A kernel change is a new
baseline and has not been installed or validated here.

- [Linux 6.6 IRQ bypass](https://github.com/torvalds/linux/blob/v6.6/virt/lib/irqbypass.c)
- [Linux 6.18 IRQ bypass](https://github.com/torvalds/linux/blob/v6.18/virt/lib/irqbypass.c)

Readiness probing is a second target: determine the actual failed probe, retry
period, and guest/network readiness before changing waits. Preserve readiness
semantics. The three-second delay is observed; its underlying cause is not yet
isolated. Do not label the remaining restore delay an unavoidable RAM-copy cost.

## Evidence and continuation

Root on `kunpeng`: `/data/clawbox-create-placement-20261027`.

- `source/`: native build sources, retaining earlier optimizations and diagnostic
  restore-phase logging; `install-result.json`, `install-shim-v2.json`: binary identities.
- `smoke-node{0,1}-v2.log`: successful real-VM checks.
- `deployment-validation/validation.json`, `tests.log`, `prepare-*.log`: Linux checks.
- `baseline-analysis/native-final.json`: baseline phase distributions.
- `c1280-r2/pre-abort-analysis.json`, `pre-abort-state.json`, `native-final.json`,
  `vmm-slowest-extract.log`, `abort.log`: candidate metrics, censoring, native evidence, cleanup.
- `c1280-r2/runs/` and `c2560/runs/`: unique frozen specs, receipts, event streams,
  ownership journals, ledgers, worker/launcher logs, matrices and run state.
- `diagnostics/attachment-1793090526.*`: 45-second c1280 trace, no cpuset migration
  submissions, six other attachment calls taking 6-13 microseconds, zero overruns.
- `c2560/diagnostics/irq-1793091628.*`: IRQ durations and blocked-thread evidence.
- `c2560/diagnostics/irq-create-correlations.json`, `host-irqbypass.h`: joined
  critical-path measurements and the installed kernel's data-structure layout.
- `c2560/pre-abort-analysis.json`, `pre-abort-state.json`, `native-final.json`,
  `abort.log`, and root `cleanup-final.json`: final 2560 results and cleanup.

Native Cubelet omits successful phase logs under 5 ms, so distributions derived
from those phase logs are conditional on logging. The shim placement/VM-start
intervals use timestamped messages and cover all matched completed creations.
Git contains source and this handoff; raw experiment artifacts remain on the host.
