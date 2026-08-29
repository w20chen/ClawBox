# High-density Firecracker trace replay

This prototype re-executes recorded shell tools while replacing each LLM call
with its recorded duration. `--tool-time-scale 1` also pads a completed real
tool invocation to its recorded phase duration, which keeps the replay timeline
stable when a disposable image completes a command faster than the source run.
The snapshot decision uses a request-length model
fitted on separate calibration traces; it does not use the current response or
recorded evaluation latency. With no `--calibration`, a fixed cold-start model
is used. `resident` and `snapshot` modes use the same limit on how many VMs may
remain in memory at once.

The current implementation also boots a small stateful agent as PID 1 inside
the Runtime microVM. The host talks to it through Firecracker vsock. Before a
checkpoint it records the in-flight inference request, predicted latency,
simulated GPU identifier, and simulated KV-cache size. After restore the replay
fails unless the guest boot nonce and in-flight request are unchanged. This
turns snapshot continuity into an asserted property rather than assuming that
a newly booted dummy VM is equivalent.

Tool commands are deliberately executed in a disposable paired workspace (or
Tool pod), then acknowledged to every configured state-checking guest. They are
not executed by the minimal PID-1 program. This keeps the prototype focused on
VM memory reclamation; it is not an in-guest shell runtime.
The inference side is behind `InferenceProvider`, so trace sleep can later be
replaced by a real GPU client without changing lifecycle decisions. Current GPU
and KV handles are simulated metadata only.

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
  "boot_args": "console=ttyS0 reboot=k panic=1 pci=off rw init=/clawbox-runtime-agent clawbox.touch_mib=128",
  "tap_device": "fc000",
  "guest_mac": "06:00:ac:10:00:02",
  "vsock_uds": "/run/clawbox-replay/session-000/runtime.vsock",
  "guest_cid": 3,
  "guest_agent_port": 18080,
  "cpu_set": "0",
  "numa_node": 0,
  "log_path": "/var/lib/clawbox-replay/session-000/firecracker.log"
}
```

Build the experiment rootfs from the Kata disk image on the target
architecture. The builder statically compiles the guest agent, extracts the
first MBR partition, and injects `/clawbox-runtime-agent`:

```bash
python scripts/build-runtime-agent-rootfs.py \
  --base-image /opt/kata/share/kata-containers/kata-containers.img \
  --agent-source clawbox/replay/guest_agent.c \
  --output-rootfs /experiment/runtime-agent-rootfs.ext4
```

Generate isolated sessions with `--guest-agent`. `--guest-touch-mib` creates a
controlled resident working set and re-touches it after each tool phase, which
prevents sparse guest memory from making the resident baseline unrealistically
cheap:

```bash
python scripts/prepare-high-density-experiment.py \
  --output /experiment/resident-input --sessions 8 \
  --workspace-source /experiment/workspace-base --base-commit "$COMMIT" \
  --rootfs-source /experiment/runtime-agent-rootfs.ext4 \
  --trace /experiment/trace.jsonl --calibration /experiment/calibration.jsonl \
  --memory-mib 512 --cpu-first 0 --guest-agent --guest-touch-mib 384
```

`scripts/firecracker-runtime-continuity-smoke.py` is a fail-closed preflight:
it snapshots and kills Firecracker, verifies zero process RSS, restores, checks
the boot nonce and in-flight inference state, and continues the next tool turn.

### Optional Tool-sandbox reclamation

An experiment session can provide `tool_firecracker_config` in addition to
`firecracker_config`. The second VM has its own guest agent, vsock, writable
rootfs, memory-residency limit, snapshot files, and CPU. The rule is kept
intentionally simple:

```text
effective predicted LLM time >= tool snapshot threshold
and effective predicted LLM time >= snapshot + restore + refault + margin
```

Runtime and Tool thresholds are independent. The defaults are 20 seconds for
Runtime and a more conservative 30 seconds for Tool, and both are configurable.
During a selected LLM phase the engine marks the request in both guests, saves
and stops the selected VMs, restores them when inference completes, verifies
both boot nonces and in-flight request IDs, and only then executes the next
real tool command.

Generate paired configs by adding a Tool rootfs and working-set size:

```bash
python scripts/prepare-high-density-experiment.py \
  ... --guest-agent --guest-touch-mib 128 \
  --tool-rootfs-source /experiment/runtime-agent-rootfs.ext4 \
  --tool-guest-touch-mib 256

python -m clawbox.replay.cli experiment manifest.json \
  --mode snapshot --resident-slots 4 --tool-resident-slots 4 \
  --snapshot-threshold-s 4 --tool-snapshot-threshold-s 10 \
  --output-dir results/paired
```

This is a direct-Firecracker Tool-sandbox prototype. The actual tool command
continues to run through the configured local, SSH, or Kubernetes executor and
is acknowledged to both guests. It does not transparently stop a production
Kubernetes Pod or reconnect a containerd/Kata shim; that remains a production
integration gap.

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

## Kunpeng stateful result (2026-08-29)

The final feasibility run used Firecracker 1.12.1 on Kunpeng. The runner had an
80-CPU Kubernetes quota and the replay process was bound to NUMA node 0, CPUs
0-79, with `set_mempolicy(MPOL_BIND)`. Eight sessions used four resident slots,
one vCPU, a 512 MiB VM, and a touched 384 MiB guest working set. Both arms used
fresh rootfs/workspace copies. Each session replayed 24 LLM and 23 real tool
actions from `12rambau__sepal_ui-747`; `12rambau__sepal_ui-774` was independent
predictor calibration. LLM replay used the recorded 104.911 seconds at scale
1.0. All 192 optimized LLM phases were snapshotted based on a 3.0 second
predicted-latency threshold, not the recorded response latency.

| Metric | Resident | Snapshot | Change |
|---|---:|---:|---:|
| Completed sessions | 8/8 | 8/8 | equal |
| Failures / tool exit mismatches | 0 / 0 | 0 / 0 | equal |
| Wall time | 213.465 s | 152.687 s | -28.5% |
| Throughput | 134.917 sessions/h | 188.621 sessions/h | +39.8% |
| Mean Firecracker RSS | 1714.9 MiB | 379.3 MiB | -77.9% |
| P95 Firecracker RSS | 1730.1 MiB | 1066.8 MiB | -38.3% |
| Peak Firecracker RSS | 1730.1 MiB | 1844.2 MiB | +6.6% burst |
| Mean resident VMs | 3.99 | 1.79 | -55.3% |
| RSS-time | baseline | optimized | -84.2% |

Each full snapshot averaged 0.301 seconds and restore API time averaged 0.046
seconds. The optimized peak is higher because concurrent full snapshots fault
guest pages before Firecracker exits. A production controller needs a separate
snapshot-I/O concurrency limit; mean/P95/RSS-time show the density benefit,
while peak RSS remains a hard burst constraint.

The latency model fitted on this calibration trace predicts a narrow 3.23-3.41
second range and is intentionally simple. It is adequate to demonstrate a
non-oracle decision path, not a strong latency-prediction result. The policy now
accounts for configured snapshot, restore, refault, and safety costs, and bases
decisions on scaled provider wait time. Prediction accuracy and online refault
estimation remain paper follow-up work.

Raw summaries and sampled RSS files are kept in the Kunpeng experiment
directory; the compact checked-in record is
`docs/results/high-density-kunpeng-2026-08-29.json`.

### Paired Runtime/Tool validation

A follow-up Kunpeng smoke replay configured a 128 MiB Runtime working set and a
256 MiB Tool working set. At `sleep-scale=0.1`, Runtime threshold 0.300 seconds,
and Tool threshold 0.335 seconds, the request-length predictor selected all 24
Runtime phases but only 6 longer-predicted Tool idle phases. All 47 actions and
23 real tools completed with zero mismatches; Tool state reached turn 24 and
all six Tool restores occurred before the following tool command. Tool
checkpoint averaged 0.264 seconds and restore averaged 0.045 seconds.

The scaled smoke reduced mean combined Firecracker RSS by 43.0%, but its wall
time was worse because 0.1-scale inference windows do not amortize 30 total
snapshots. It validates the rule and continuity, not a throughput claim. Use
scale 1.0 and repeated trials for a performance result.
