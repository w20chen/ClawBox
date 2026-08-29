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

For a paired direct-Firecracker run, Tool commands execute in the Tool VM's
PID-1 guest agent through vsock. Commands and output are base64-framed and the
host opens a new vsock connection for every command, so no pre-snapshot
connection is reused after restoration. The Tool agent rejects execution while
an LLM request is marked in flight. Local, SSH, and Kubernetes executors remain
available only for single-VM/legacy comparison runs and are rejected when a
paired Tool Firecracker configuration is supplied.
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

## Self-service paired experiment

`scripts/run-direct-firecracker-experiment.sh` is the shortest reproducible
entry point. It refuses an existing output directory, builds a fresh agent
rootfs with a copy of the specified disposable workspace at `/workspace`,
creates independent Runtime and Tool rootfs copies, and runs one arm. Invoke it
once per arm with identical inputs; do not reuse either output directory.

```bash
BASE_COMMIT="$(git -C /experiment/workspace-base rev-parse HEAD)"

bash scripts/run-direct-firecracker-experiment.sh \
  --mode resident --output /experiment/direct-resident \
  --base-image /opt/kata/share/kata-containers/kata-containers.img \
  --workspace /experiment/workspace-base --base-commit "$BASE_COMMIT" \
  --trace /experiment/trace-747.jsonl --calibration /experiment/calibration-774.jsonl \
  --sessions 1 --memory-mib 512 --sleep-scale 1 \
  --snapshot-threshold-s 3 --tool-snapshot-threshold-s 3 \
  --estimated-snapshot-s 0.3 --estimated-restore-s 0.1 \
  --estimated-refault-s 0.5 --safety-margin-s 2
```

Repeat with `--mode snapshot --output /experiment/direct-snapshot` and compare
the two `results/summary.json` and `results/rss.json` files. The script leaves
the generated images and event JSONL files in its output directory for audit.
For a fast functional smoke, generate a two-request/two-tool trace with
`python3 scripts/make-direct-replay-smoke.py --output /tmp/direct-smoke.jsonl`
and pass that same file as both `--trace` and `--calibration`.

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
and stops the selected VMs, restores the Tool VM first, waits for its vsock
health check, then restores Runtime. It verifies both boot nonces and in-flight
request IDs before opening a new Tool-vsock connection for the next real tool
command.

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

This is a direct-Firecracker Tool-sandbox prototype. A paired manifest uses
`"tool_transport": "vsock"`; the runner rejects local, SSH, and Kubernetes
tool transports in that mode so a Tool VM cannot merely stand in for memory.
The Tool rootfs must be a private writable image containing the task workspace
at `/workspace`. It does not transparently stop a production Kubernetes Pod or
reconnect a containerd/Kata shim; those remain future work. Snapshot files
should live on local NVMe; putting them on tmpfs defeats the memory-density
measurement.

The lifecycle alternates the configured snapshot paths with a `.next` pair.
This is required because a restored VM memory-maps its current memory snapshot;
overwriting that file during the next checkpoint can corrupt the VM. The old
pair is removed only after its Firecracker process has exited.

When `numa_node` and `cpu_set` are configured, the lifecycle uses a small
`set_mempolicy(2)` plus `sched_setaffinity(2)` exec wrapper. This avoids loading
a host `numactl` binary into a guest/container with an incompatible glibc.

For the two-direct-Firecracker path, pass both private configs and use vsock:

```bash
python -m clawbox.replay.cli run trace.jsonl --backend firecracker \
  --firecracker-config runtime-000.json --tool-firecracker-config tool-000.json \
  --tool-transport vsock --mode snapshot --events results/session-000.jsonl
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
   "firecracker_config": "session-000.json",
   "tool_firecracker_config": "tool-000.json", "tool_transport": "vsock"}
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

### Historical paired Runtime/Tool state-continuity smoke

Before the direct Tool-vsock executor was added, a Kunpeng smoke replay
validated paired VM state continuity (128 MiB Runtime, 256 MiB Tool, six Tool
restores), but dispatched its commands through a host-side transport. Its 43.0%
mean combined-RSS reduction is retained in
`docs/results/paired-tool-reclamation-kunpeng-2026-08-29.json` only as a
historical state-continuity observation, not as validation of the current
end-to-end Tool execution path. Re-run the paired resident/snapshot protocol
above on Kunpeng before citing two-VM command-continuity performance results.
