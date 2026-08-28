# High-density Firecracker trace replay

This prototype re-executes recorded shell tools while replacing each LLM call
with its recorded duration. `--tool-time-scale 1` also pads a completed real
tool invocation to its recorded phase duration, which keeps the replay timeline
stable when a disposable image completes a command faster than the source run.
The snapshot decision uses a request-length model
fitted on separate calibration traces; it does not use the current response or
recorded evaluation latency. With no `--calibration`, a fixed cold-start model
is used. `resident` and `snapshot` modes use the same explicit resident-slot
budget.

The production `SandboxTask` CRD is deliberately unchanged. This experiment
uses Firecracker's API directly so a full snapshot followed by process exit is
measured before a reversible lifecycle is promised by the Kubernetes API.

## Trace validation

Both agent-test-bench action-v4 JSONL and ClawTune span-v6 JSONL are accepted.
Incomplete v6 spans and tools without a shell command fail closed.

```bash
python -m clawbox.replay.cli inspect trace.jsonl --calibration older-trace.jsonl
```

For one dry replay on the host (use only in a disposable workspace):

```bash
python -m clawbox.replay.cli run trace.jsonl \
  --backend local --mode snapshot --sleep-scale 0 \
  --cwd /tmp/disposable-replay --events /tmp/replay.jsonl
```

## Direct Firecracker configuration

Each concurrent session needs a private writable rootfs, API socket, snapshot
files, TAP device, forwarded SSH port, and CPU set. Example config:

```json
{
  "binary": "/usr/local/bin/firecracker",
  "api_socket": "/run/clawbox-replay/session-000/api.sock",
  "kernel_image": "/var/lib/clawbox-replay/vmlinux",
  "rootfs": "/var/lib/clawbox-replay/session-000/rootfs.ext4",
  "snapshot_state": "/var/lib/clawbox-replay/session-000/snapshot.vmstate",
  "snapshot_memory": "/var/lib/clawbox-replay/session-000/snapshot.mem",
  "vcpu_count": 1,
  "memory_mib": 2048,
  "tap_device": "fc000",
  "guest_mac": "06:00:ac:10:00:02",
  "cpu_set": "0",
  "numa_node": 0,
  "log_path": "/var/lib/clawbox-replay/session-000/firecracker.log"
}
```

With `--tool-transport ssh`, the guest image must boot networking and SSH
without host-side cloud-init. TAP,
routes, NAT/port forwarding, private rootfs copies, and SSH keys must be
prepared before the run. Snapshot files should live on local NVMe; putting them
on tmpfs defeats the memory-density measurement.

The lifecycle alternates the configured snapshot paths with a `.next` pair.
This is required because a restored VM memory-maps its current memory snapshot;
overwriting that file during the next checkpoint can corrupt the VM. The old
pair is removed only after its Firecracker process has exited.

When `numa_node` and `cpu_set` are configured, the lifecycle uses a small
`set_mempolicy(2)` plus `sched_setaffinity(2)` exec wrapper. This avoids loading
a host `numactl` binary into a guest/container with an incompatible glibc.

For the native ClawBox two-VM Cell layout, prefer `--tool-transport kubectl`:
the directly managed Runtime microVM is checkpointed during LLM sleep, while
the recorded tools run again in its paired Kata/Firecracker Tool pod.

```bash
python -m clawbox.replay.cli run trace.jsonl --backend firecracker \
  --firecracker-config runtime-000.json --tool-transport kubectl \
  --tool-namespace clawbox-benchmarks --tool-pod replay-tool-000 \
  --mode snapshot --events results/session-000.jsonl
```

Single-session command:

```bash
numactl --cpunodebind=0 --membind=0 python -m clawbox.replay.cli run trace.jsonl \
  --backend firecracker --firecracker-config session-000.json \
  --ssh-port 22000 --ssh-identity replay_ed25519 \
  --calibration calibration.jsonl --mode snapshot \
  --events results/session-000.jsonl
```

## Controlled capacity experiment

An experiment manifest has one independently provisioned session per entry:

```json
{"sessions": [
  {"trace": "trace.jsonl", "calibration": ["calibration.jsonl"],
   "firecracker_config": "session-000.json", "tool_transport": "kubectl",
   "tool_namespace": "clawbox-benchmarks", "tool_pod": "replay-tool-000"}
]}
```

Run fresh copies twice. Do not reuse mutated rootfs files between arms.

```bash
numactl --cpunodebind=0 --membind=0 python -m clawbox.replay.cli experiment resident.json \
  --mode resident --resident-slots 16 --numa-node 0 --output-dir results/resident
numactl --cpunodebind=0 --membind=0 python -m clawbox.replay.cli experiment snapshot.json \
  --mode snapshot --resident-slots 16 --numa-node 0 --output-dir results/snapshot
```

Use CPUs and memory from one `numactl --hardware` node only. The same slot
count, VM size, trace population, rootfs source, calibration data, and threshold
must be used for both arms. Report completed sessions/hour, peak summed
Firecracker RSS, peak resident VMs, snapshot/restore overhead, failures, and
tool exit mismatches. Run at least three repetitions after one warm-up and
report medians plus ranges. Keep host page cache policy identical across arms;
the lifecycle applies `POSIX_FADV_DONTNEED` to snapshot files after eviction.

## Kunpeng prototype result (2026-08-29)

This preliminary paired run used NUMA node 0 (CPUs 0-79), Firecracker 1.12.1,
eight logical sessions, four resident slots, one vCPU and 512 MiB per Runtime
VM. It replayed the 47-action `12rambau__sepal_ui-747` trace: 24 LLM phases
(104.911 recorded seconds) and 23 tool phases (372.020 recorded seconds).
`12rambau__sepal_ui-774` supplied 37 independent predictor calibration samples.
The fitted request-length model had a 3.1445 second intercept and
3.026e-6 seconds/input-character slope; it never inspected evaluation-response
length or evaluation latency when making the snapshot decision.

| Metric | Resident | Snapshot | Change |
|---|---:|---:|---:|
| Completed sessions | 8/8 | 8/8 | equal |
| Failures / exit mismatches | 0 / 0 | 0 / 0 | equal |
| Wall time | 955.879 s | 814.218 s | -14.8% |
| Throughput | 30.129 sessions/h | 35.371 sessions/h | +17.4% |
| Mean Firecracker RSS | 408.7 MiB | 110.2 MiB | -73.0% |
| P95 Firecracker RSS | 420.0 MiB | 184.4 MiB | -56.1% |
| Peak Firecracker RSS | 420.0 MiB | 1848.3 MiB | snapshot burst |
| Snapshot cycles | 0 | 192 | +192 |

The optimized arm reduced integrated Firecracker RSS-time by about 77% and
resident-VM time by about 20%. Its peak RSS was worse because concurrent full
snapshot creation faults guest memory into host RSS; mean/P95 and RSS-time are
the density metrics, while peak must be reported as a burst constraint. Across
the optimized sessions, each checkpoint averaged roughly 0.31 seconds and each
restore roughly 0.046 seconds.

The exact historical x86 task image was no longer cached on the arm64 host.
Every recorded tool command was still executed against a fresh checkout and
exit codes were validated, but unavailable task dependencies made some commands
finish faster; `--tool-time-scale 1` preserved their recorded phase durations.
This is suitable for the Runtime-VM density prototype, not a claim of exact
tool CPU-cycle reproduction. A paper-quality result should repeat the paired
trial and use an archived arm64 task image when CPU-load fidelity is evaluated.
