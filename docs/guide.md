# Experiment guide

This guide covers the supported standalone workflow. Run commands on the ARM64
Linux/KVM experiment host from the repository checkout. `scripts/clawbox` loads
the saved machine environment and project Python environment; place `--sudo`
before `experiment` for privileged operations.

## Files and units

| File | Purpose |
| --- | --- |
| `~/.config/clawbox/machine.env` | API addresses, source paths, immutable image references, template IDs, and default output paths |
| `~/.config/clawbox/host.yaml` | Editable NUMA, CPU, memory, storage, and host-network plan |
| `~/.config/clawbox/host.json` | Applied profile written by `host apply`; use it as `configure --profile`, do not edit it as the plan |
| Experiment YAML | Workload, VM identities, policies to compare, concurrency, and timeouts |
| `RUN/experiment.yaml` | Frozen specification for one run ID; never edit it in place |

Host capacities use GiB. Experiment VM memory, reservations, and watermarks use
MiB. Result names ending in `_bytes` use bytes. Relative paths resolve from the
current directory; use absolute paths for traces, prediction files, and results.

The installed CubeSandbox SDK must include `sdk-control-timeouts.patch`,
`sdk-lifecycle-timeouts.patch`, and `sdk-filesystem-timeouts.patch`. Follow the
[SDK update commands](installation.md#what-an-update-rebuilds) after updating an
existing host. Its request timeout defaults to 30 seconds per
create, resume, pause, status, or destroy request; it is not the experiment
deadline. After an ambiguous create failure, preserve the run directory and use
`experiment abort RUN_ID` before starting a different run.

## Configure or recover a host

Inspect the real topology before creating a plan:

```bash
cd "$HOME/src/ClawBox"
bash scripts/clawbox experiment host inspect
bash scripts/clawbox experiment host init \
  --detect --output "$HOME/.config/clawbox/host.yaml"
```

`--detect` proposes values; it does not establish available capacity. Review the
file before applying it. Important host fields are:

| Field | Meaning |
| --- | --- |
| `compute_nodes[].numa_node`, `cpus` | NUMA node and logical CPU list; compute domains must not overlap |
| `memory_gib`, `low_gib`, `high_gib` | Per-domain capacity and watermarks; require `0 < low < high < memory` |
| `warm`, `warm_node`, `warm_gib` | Shared in-memory checkpoint/borrow pool |
| `shared_borrow_percent` | Maximum live-VM share of the common pool, from 0 to 100. Borrow and WARM-snapshot reservations still share one hard `warm_gib` capacity; 100 lets live VMs use otherwise-idle capacity, while tiered snapshots fall back to COLD when the common pool is full. |
| `warm_root`, `cold_root`, `output_root` | Dedicated WARM tmpfs, COLD snapshot, and result paths |
| `minimum_disk_free_gib` | Required free space; CubeSandbox also rejects snapshot writes above 85% filesystem use |
| `tcp_tw_reuse` | `null` preserves the host value; `0`, `1`, and `2` request Linux's documented modes |
| `cube_proxy_connect_timeout_seconds` | Guest HTTP, streaming, and gRPC connection timeout, 3–300 seconds; `null` preserves Cube's default. Use 30 for saturated Full runs. The separate internal resume-sidecar timeout stays at 2 seconds. |
| `cubelet_tap_init_num` | Optional warm TAP target, not a VM limit. The patched embedded runtime creates a TAP on demand when the ready pool is empty. Size this for the expected creation burst, not all offered sessions; a very large idle pool makes restart recovery proportional to the retained TAP count. Changing it requires an idle-host apply and a new run ID. |
| `cube_scheduler_overcommit` | Optional map from Cube node `InstanceType` to positive `cpu_ratio` and `mem_ratio`; `null` preserves Cube's settings. These multiply Cube's reported configured-VM quotas, not physical CPU/RAM allocations. |
| `cube_scheduler_vm_count_ratio` | Optional positive per-instance multiplier over Cube's node-reported VM-count limit; `null` preserves Cube's setting. This is a scheduler limit, not physical capacity. |
| `cube_master_log_level` | Optional CubeMaster file-log threshold: `debug`, `info`, `warn`, or `error`; `null` preserves Cube's setting. ClawBox writes Cube's required uppercase value. Use `warn` for large runs to retain warning/error evidence without logging every template and sandbox-list response. Changing it requires idle-host apply and a new run ID, but no image rebuild or retraining. |

Check without mutation, then apply on an idle host:

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply "$HOME/.config/clawbox/host.yaml"
```

Apply again after reboot or after changing topology, CPU lists, memory capacity,
mounts, or host networking. `host apply` writes the adjacent `host.json` only
after service, storage, and real-VM probes succeed. While Cube is idle, apply
also releases clean file cache retained in Cube's reusable VM cgroups so an old
run cannot silently reduce the next run's LOCAL budget.

Changing `cube_proxy_connect_timeout_seconds` restarts CubeProxy on an idle host
and requires a new run ID. It rebuilds neither CubeSandbox nor Runtime/Tool
images or templates, and it does not change an experiment policy or budget.

`host check` also verifies that the installed Python SDK bounds create and
inventory HTTP requests. An SDK-only timeout repair requires reinstalling the
patched SDK and starting a new run ID; it does not require rebuilding VM images.

For high-density runs, check Cube's separate configured-VM quotas as well as
ClawBox's measured admission limits. `launch` checks a conservative envelope of
all offered Runtime/Tool pairs before qualification; heterogeneous cases use the
largest Tool specification. Existing Cube allocations and per-instance-type
overrides count. A quota rejection is not evidence that physical RAM or CPU is full.
For example, **only if the node reports `InstanceType: cubebox`**, an explicit plan is:

```yaml
cube_scheduler_overcommit:
  cubebox:
    cpu_ratio: 10.0
    mem_ratio: 3.0
cube_scheduler_vm_count_ratio:
  cubebox: 1.05
```

Choose multipliers from the reported node quotas and requested VM specifications,
allowing headroom because Cube rejects equality. Use the same infrastructure
quotas across compared baselines. `host apply` backs up CubeMaster's configuration
and restarts **CubeMaster only when these values change**; apply on an idle host.
The setting affects all nodes of that instance type served by this CubeMaster.
It does not change the experiment CPU set, LOCAL/shared budgets, physical-memory
safety checks, or VM sizes. No image rebuild or KB retraining is needed; use a
new run ID for the changed experimental conditions.

Crash-dump cleanup is opt-in and narrow:

```bash
bash scripts/clawbox --sudo experiment host clean
bash scripts/clawbox --sudo experiment host clean --apply
```

The first command is a preview. The command only selects recognized, old,
non-growing dump files and never prunes Docker volumes, templates, results, or
arbitrary large files.

## Create an experiment

New experiments use `schema_version: 4`. The standard VM sizes are Runtime
1024 MiB (1 GiB) and Tool 3072 MiB (3 GiB), for 4 GiB per session.
At a 0.75 ledger scale, the pair is charged 3072 MiB (3 GiB). Start from a checked-in example or a
previous experiment, then inject the applied host identity:

```bash
bash scripts/clawbox experiment recipes
bash scripts/clawbox experiment configure \
  examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/eval.yaml" \
  --profile "$HOME/.config/clawbox/host.json" \
  --recipe custom --concurrency 1 --repetitions 1
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/eval.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/eval.yaml" --inputs
```

`configure` never resizes registered templates. Register new templates before
changing VM vCPU, RAM, disk size, image, guest dependencies, or workspace
contents. Use `--force` only to replace an output YAML, never to reuse a run ID.
Each policy variant is stored as a comparison `arm` in the YAML and results.

The schema has six mechanism dimensions plus deployment and coordination:

| Mechanism | Main choices | What it controls |
| --- | --- | --- |
| `admission` | `off`, `fixed`, `predicted` | Whether a tool command may start; reservations do not resize guest RAM |
| `runtime` | memory protection, CPU release boost, proactive reclaim | Per-VM reaction to measured host contention |
| `placement` | round-robin, explicit, OS default, load-aware, optional one-cache-cluster mode (`c2`) | Session NUMA placement and eligible tool-call CPU affinity |
| `idle_reclaim` | `off`, `wait`; tool or agent-and-tool scope | Checkpointing while a real model request is outstanding |
| `shared_memory` | `off`, `hard`, `pressure` | Temporary placement of running VM memory in the shared NUMA pool |
| `checkpoint` | full-copy or incremental CoW; disk, WARM, or tiered | Snapshot encoding, storage, and residency |
| `deployment` | `managed`, `os-default` | Managed lifecycle accounting versus the guarded OS baseline |
| `coordination` | `reclaim_first`, `measured_cost` | Choice between safe reclaim and borrowing actions |

Managed placement reuses CubeSandbox's normal preallocated per-VM cgroup pool;
ClawBox does not create a parallel VM-cgroup hierarchy. An opaque placement
token carries the selected NUMA-node intent, Cubelet publishes the official
assigned leaf, and CubeShim enters that leaf before launching the VMM. The
leaf's native cgroup-v2 memory-node constraint applies to the VMM and its
descendants, while CubeShim sets NUMA CPU affinity before spawning VMM threads.
No redundant per-thread memory policy is needed. ClawBox then verifies vCPU
affinity and physical
memory residency and attaches pressure, borrowing, and `c2` controls to that
official leaf. Live borrowing uses temporary threaded cpusets inside that leaf
to migrate each VM once per process, retaining the original memory accounting
and restoring its CPU affinities. The temporary groups are removed before the
operation completes. This avoids repeated per-thread migration on older Linux
kernels and requires no image rebuild or ClawTune retraining. CubeSandbox
clears the leaf's `cpuset.mems` when it returns to its pool. This integration
changes VM creation mechanics, not the admission, prediction, borrowing,
checkpointing, pressure-control, or tool-call placement strategies.

Use `configure --fixed-methods --recipe FAMILY` to compare one policy family
while keeping the workload, host resources, measurements, and numeric settings
fixed. Always inspect `describe`: names and labels do not change runtime behavior.
These comparisons use the full selected NUMA CPU pool (`c0`) unless the generated
configuration explicitly says otherwise.

`comparison.arms[].override` accepts dotted mechanism fields. Environment,
workload, hardware limits, and evidence remain common across arms. Arm overrides
take precedence over common `--set` values; `configure` warns when an explicit
setting is overridden and lists the affected arms and their effective values. Invalid
combinations fail validation instead of silently disabling a mechanism.

Use these interfaces instead of copying field lists from documentation:

```bash
bash scripts/clawbox experiment schema
bash scripts/clawbox experiment configure --help
bash scripts/clawbox experiment describe --help
```

For predicted admission, `--prediction-mode cold-start` uses the immutable
ClawTune `bootstrap-v2` seed directly across workload repositories. Set
`resources.prediction_artifact` to the seed directory. It does not require a
ClawBox-specific seed, prior workload replay, or VM calibration in the seed.

```bash
export CLAWTUNE_ROOT="$HOME/src/ClawTune"
python scripts/evaluate-cold-start-coverage.py \
  --seed "$CLAWTUNE_ROOT/seeds/bootstrap-v2" \
  --workload /path/to/workload.json \
  --output /path/to/new-coverage.json
```

Use `clawbox experiment configure` with `--prediction-mode cold-start` and
`--prediction-artifact "$CLAWTUNE_ROOT/seeds/bootstrap-v2"`, together with the
host and workload options shown above. The coverage command starts no VMs;
its report measures prediction availability, not held-out accuracy.

Cold-start admission queries `cgroup_v2_environment_union_v1` memory evidence.
It selects Lattice extra-memory P50, then Lattice sampled RSS, then ToolKB
extra-memory and tool-class extra-memory. The selected value is used directly
as a host-growth estimate, not a measured host calibration or guest-memory
measurement. `resources.prediction_reservation_scale` (CLI
`--prediction-reservation-scale`, default `1.0`, positive dimensionless multiplier)
adjusts the reservation; the final reservation is rounded up to whole MiB, with
a minimum of 0 MiB. A zero prediction therefore reserves 0 MiB.
Prediction records retain the original measurement, assumptions, selected model,
and `identity_estimate` mapping. Zero predictions and partial evidence are accepted.

`--prediction-mode runtime` continues to use an immutable KB trained by
`clawbox experiment train` for the matching repository and Tool image, including
its measured guest-to-host calibration. `frozen` uses its precomputed commands.
Training accepts partial, approximate and short observations; ClawTune supplies
missing-target zeros. Quality and interruption markers remain diagnostics.
Execution identity, artifact format, policy failures and cleanup remain checked.

The deployment seed contains former evaluation tasks. Do not treat accuracy on
those tasks as held-out accuracy. Changing prediction input, reservation scale,
or training policy requires a new run ID; completed results remain unchanged.

ClawTune owns the seven-target prediction/measurement contract: duration, cumulative CPU, average CPU cores, 500 ms peak CPU cores, sampled process RSS, environment total peak and environment extra peak. ToolKB, TrieKB and LatticeKB support all seven; EdgeKappa predicts duration only. The main memory-admission workflow does not consume CPU, duration or PMU predictions; PMU and disk/network measurements remain in the traces. The default seed is resolved by ClawTune (`bootstrap-v2`); `CLAWTUNE_KB_SEED` selects another seed when initializing new state. Existing writable state is not replaced. Use a new state directory and run ID to adopt a new seed. Collector code changes require rebuilding the Runtime/Tool images; selecting a different external seed alone does not.

### Compare fixed memory charges

`--recipe resident-capacity` generates three supported arms:

| Arm | Charge per VM |
| --- | --- |
| `resident-capacity-1` | configured RAM * 1.0 |
| `resident-capacity-0-8` | configured RAM * 0.8 |
| `resident-capacity-0-75` | configured RAM * 0.75 |

These arms keep both VMs resident, disable checkpointing and adaptive memory
policies, and use round-robin placement over the full selected NUMA CPU pool.
The multiplier changes only the accounting charge; it does not resize VM memory,
change CPU lists, or change vCPU counts.
Each VM's charge is rounded up separately to MiB; a session reserves the sum of
its Agent and Tool charges until cleanup, including model waits. For example,
a 6144 MiB VM is charged 4916 MiB at 0.8 or 4608 MiB at 0.75. Existing charges
plus the new pair's charge must fit the configured per-domain budget. This recipe
does not use measured RSS or LOW/HIGH watermarks to admit work, although those
measurements remain in the results. VM creation and kernel memory failures are
still experiment outcomes.

```bash
bash scripts/clawbox experiment configure \
  examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/resident.yaml" \
  --profile "$HOME/.config/clawbox/host.json" \
  --recipe resident-capacity --concurrency 32
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/resident.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/resident.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/resident.yaml" \
  --run-id resident-capacity-01
```

The concurrency value is offered session concurrency, not a guaranteed number
of resident pairs. Capacity accounting, per-node budgets and CPU placement limits
can reduce simultaneous residency. The recipe sets
startup headroom to zero; it does not enlarge the host's available memory budget.
Use a host-configured workload and registered VM templates of the desired sizes.
To run only the discounted arms, remove `resident-capacity-1` from the generated
`comparison.arms` before validation and launch.

`execution.sandbox_create_concurrency` limits how many Runtime/Tool VM pairs are
created at once (default `8`; unit: session pairs). The two VMs in each admitted
pair start concurrently, so maximum Cube create-request concurrency is twice this
value. Create requests also share the Cube lifecycle-operation budget described
below. This field does not limit tool calls.
Set it no higher than the largest `execution.concurrency_levels` value. Changing
it requires validation and a new run ID, but not host apply, image rebuild,
qualification, or ClawTune retraining.

`mechanisms.checkpoint.pool_transfer_concurrency` is the shared Cube lifecycle
operation budget (default `1`; unit: concurrent VM operations). Create,
checkpoint, restore, and snapshot-spill calls draw from this one pool, so idle
capacity is available to whichever operation needs it without allowing their
combined load to overwhelm Cube. `pool_transfer_timeout_seconds` bounds how long
an operation waits for a slot (default `120`; unit: seconds). Changing either
field requires validation and a new run ID, but not host apply, image rebuild,
qualification, or ClawTune retraining.

`mechanisms.checkpoint.snapshot_transfer_concurrency` optionally applies a
smaller limit to checkpoint, restore, and snapshot-spill operations within that
shared lifecycle budget (default: the value of `pool_transfer_concurrency`;
unit: concurrent VM operations). Use it when a checkpoint wave must leave Cube
lifecycle capacity for new VM creation. It must not exceed
`pool_transfer_concurrency`. Changing it requires validation and a new run ID,
but not host apply, image rebuild, qualification, or ClawTune retraining.

The shared memory capacity includes pre-existing shared-node cache and WARM
files. Each run measures this occupancy before launching VMs and withholds that
space from new snapshot and live-borrow reservations. Existing data remains in
place; the pool report distinguishes this occupancy from the run's own
reservations and committed snapshots.

NUMA rebinds are serialized before entering the kernel's ordered migration
queue. A new borrow defers to the existing pressure/reclaim policy while another
rebind is active. This does not reduce checkpoint or restore concurrency.
Borrowing requires the `numa-migration-pause.patch` Shim build and host `libnuma`
(installed with `numactl`). Cube briefly pauses the VM, preserves checkpoint
dirty bits, and detaches its KVM RAM slots during page migration and the cpuset
change. Read-only device-image mappings are faulted back from their unchanged
files, keeping their shared cache out of whole-process migration. It restores
the RAM slots and resumes the VM after destination placement
succeeds. A failed migration retains the reservation and fails the owning run
so its VM can be cleaned up. Borrow/return events report
`native_prepare_seconds`, `premigrate_seconds`, `cpuset_seconds`,
`native_resume_seconds`, and `premigrate_remaining_pages`; residual pages do
not release reserved capacity without a residency check. Updating this native
path requires installing the rebuilt Shim on an idle host and a new run ID;
it does not require VM-image rebuilds or ClawTune retraining.

`execution.dependency_install_concurrency` optionally limits concurrent `apt`,
`apt-get`, and Python `pip install` commands across an experiment arm (default:
unset/unlimited; unit: commands). Other tool commands are unaffected. Use it when
many traces would otherwise saturate the shared package-download path. Changing
it requires validation, qualification, and a new run ID; it does not require host
apply, image rebuild, or ClawTune retraining.

For a repeated Debian/Ubuntu workload, a host-local APT cache avoids downloading
the same repository indexes and packages for every Tool VM. Start it on the
physical host address that CubeSandbox VMs can reach (port `3142` is the
default; cache data persists under `/var/lib/clawbox/apt-cache`):

```bash
sudo bash scripts/clawbox-package-cache start \
  --bind-address 193.124.7.2 \
  --port 3142 \
  --data-root /var/lib/clawbox/apt-cache
sudo bash scripts/clawbox-package-cache status
```

Then set the editable experiment field:

```yaml
execution:
  apt_cache_proxy_url: http://193.124.7.2:3142
```

The Worker writes this endpoint only to the Tool VM's APT configuration; it
does not proxy pip/HTTPS or rewrite recorded commands. The default is unset.
Changing the URL or enabling the cache requires qualification and a new run ID,
but not host apply, an image rebuild, or ClawTune retraining. The cache container
must be running before the experiment. `sudo bash scripts/clawbox-package-cache
stop` stops it without deleting the cached packages or container.

For repeated Python package installs, run a host-local devpi caching index on
the physical address reachable from CubeSandbox VMs (port `3141` is the
default; cache data persists under `/var/lib/clawbox/pypi-cache`):

```bash
sudo bash scripts/clawbox-pypi-cache start \
  --bind-address 193.124.7.2 \
  --port 3141 \
  --data-root /var/lib/clawbox/pypi-cache
sudo bash scripts/clawbox-pypi-cache status
```

Then set the editable experiment field:

```yaml
execution:
  pypi_cache_index_url: http://193.124.7.2:3141/root/pypi/+simple/
```

The Worker exports this URL only in the Tool bridge environment as pip's index
and trusted host. It does not add a global HTTP/HTTPS proxy or rewrite recorded
commands. The default is unset. Enabling or changing it requires qualification
and a new run ID, but not host apply, image rebuild, or ClawTune retraining. The
cache container must be running before the experiment. `sudo bash
scripts/clawbox-pypi-cache stop` stops it without deleting cached files or the
container.

For large simultaneous Runtime startups, set
`inference.configuration.runtime_startup_timeout_seconds` in the experiment YAML
to the sidecar readiness budget in seconds (default `60`, integer `1`–`3600`).
For example, `300` allows five minutes while still requiring the health endpoint
to succeed. Use a new run ID and qualification after changing it; no image rebuild
or host apply is needed. Failed readiness checks preserve `openclaw/SESSION/sidecar-startup.log`
when the VM remains reachable, or `sidecar-startup-unavailable.txt` otherwise.

`execution.cube_request_timeout_seconds` sets the Cube SDK HTTP request timeout
(default `130` seconds, positive). The CubeAPI server must also allow long VM
creation requests; use a server with creation on its 120-second lifecycle budget.
Changing this experiment setting requires qualification and a new run ID, without
rebuilding guest images. After an ambiguous creation timeout, ClawBox verifies a
guest command before accepting a recovered VM; a listed VM alone is insufficient.

`execution.cube_create_capacity_retry_seconds` bounds how long one VM creation
may retry CubeMaster's definite `130597` (`no more resource`) scheduler rejection
(default `60`; unit: seconds; `0` disables retries). Retries use jittered
exponential backoff and release the shared Cube lifecycle slot while waiting, so
checkpoint operations can free scheduler capacity. Other create failures still
propagate immediately, and ambiguous HTTP failures still use ownership
reconciliation rather than repeating the create request. Changing this field
requires qualification and a new run ID, but no host apply, image rebuild, or
ClawTune retraining.

Runtime completion uses Cube filesystem change notifications when snapshots are
enabled. `inference.configuration.runtime_completion_watch_seconds` sets each
notification wait (default `10` seconds, range `1`–`60`). A completion event wakes
the waiter immediately; the limit controls idle reconnects and cancellation
checks. Watches release the lifecycle lock and reconnect after checkpoint/restore.
For example, set this field to `30` to reduce idle requests further. Install the
[SDK timeout patches](installation.md#what-an-update-rebuilds) before using this
path. Changing this field requires a new run ID, but no host apply, image rebuild,
retraining or repeated capability qualification.

The patched Shim also reuses Cube's bounded guest-agent reconnect when creating
or restoring from a snapshot: up to five attempts, 200 ms between attempts,
with a 10-second overall deadline and the existing 3-second RPC timeout.
It retries only the connection and guest reset, before workload execution.
This backend fix requires rebuilding the Shim and a new run ID; it does not
require rebuilding VM images or retraining ClawTune. See the
[update reference](installation.md#what-an-update-rebuilds).

`execution.worker_thread_switch_interval_seconds` controls Python thread handoff
(default `0.001` seconds; valid range above zero through `0.1`). Keep it identical
across comparisons. Changing it requires qualification and a new run ID, but not
host apply, image rebuild, or prediction retraining.

For custom arms, the explicit fields are
`deployment.resident_capacity_accounting: true` and
`deployment.lifecycle_reservation_scale` (greater than zero and at most one).
Discounting requires managed resident capacity accounting. Fixed-charge mode
requires tool admission off and rejects shared-memory borrowing and proactive
memory reclaim, whose execution paths are incompatible with this mode. Existing experiments
retain their original accounting unless this mode is enabled. Results include
`resource_scope.session_capacity_charge_mib` and the scale for auditing.

### Tool-call CPU placement

`mechanisms.placement.tool_call_cpu_policy: c0` means that Linux schedules the
Tool VM over the full CPU list of the selected NUMA node. The alternative value,
`c2`, may temporarily limit an eligible `agent-tool` `exec` call to one
last-level-cache (LLC) cluster:

- A new call starts on the full NUMA mask.
- After `tool_call_cpu_min_duration_seconds` (default 1 second), it may narrow to
  one complete LLC cluster with enough independent physical cores.
- `tool_call_cpu_group_max_active` limits narrowed VMs per cluster group; it is
  neither exclusive ownership nor a CPU quota.
- Sustained CPU PSI above `tool_call_cpu_pressure_ratio` for
  `tool_call_cpu_pressure_samples` samples expands the call back to the full mask.
- `tool_call_cpu_include_smt: true` includes every allowed SMT sibling in the
  selected cluster. The default is `false`.
- Completion, failure, or response loss restores the full NUMA mask.

The `c2` mode requires readable Linux cache/cluster/SMT topology and at least two
eligible LLC clusters in the selected NUMA CPU pool. Filesystem calls, backend
maintenance, unknown operations, and short `exec` calls retain full-pool behavior.
Compare the two modes with identical VM resources, CPU pools, workload,
admission, and memory policy.

### Workloads and replay

Recorded nonzero shell results are accepted only when the Runtime call identity,
command, exit code, and error match the original trace. They remain recorded as
Tool failures and do not establish task correctness. New errors, timeouts, or
missing comparison evidence fail the run; final validation still applies.

The supported path uses `agent.driver: openclaw` and replay inference. A replay
trace pairs `span_start` and `span_end` records for each model or tool call. Model
responses request real tools; recorded tool output is not substituted for actual
execution.

```bash
bash scripts/clawbox experiment import-trace SOURCE.jsonl --output REPLAY.jsonl \
  --source-workspace /testbed --target-workspace /workspace
bash scripts/clawbox experiment trace REPLAY.jsonl
bash scripts/clawbox experiment validate EXPERIMENT.yaml --inputs
```

The Tool image must contain the recorded repository, revision, dependencies, and
workspace. For multiple workloads, define `workload.cases[]`; a case-level
`sandbox` is a complete VM description, not a partial override. Qualification
executes the first real tool call for every case and checks every distinct Tool
template on every selected compute node.

Baseline experiments default to reproducible Poisson arrivals and unique trace
assignment. `execution.poisson_arrival_window_fraction` defaults to `2.0`:
the expected last arrival is twice the median selected trace's recorded
wall-clock span. A run therefore normally spans about three trace durations,
with overlapping sessions but enough time for bounded VM creation to keep up.
The realized exponential inter-arrivals are derived
from `random_seed`, repetition, concurrency, and ordered case IDs; policy names
are excluded so every compared baseline receives the identical arrival sequence.
Set the optional `execution.poisson_arrival_window_seconds` (CLI
`--poisson-arrival-window-seconds`) to a positive number of seconds when multiple
scales must use one common expected window. It overrides the fraction without
rescaling the realized exponential sequence; the actual last arrival normally
differs from the configured expectation. Changing either arrival-window field
requires validation, qualification, and a new run ID. It does not require host
apply, an image rebuild, or prediction retraining.
Set `arrival_schedule: burst` only for an explicit burst experiment, or
`fixed_stagger` with `stagger_interval_seconds` for a fixed-rate experiment.
The default `execution.arm_timeout_seconds` is `3600` seconds. This is a failure
deadline, not an added delay; increase it for traces whose expected arrival
window plus maximum trace duration can exceed one hour.

For a fixed task count `N > 1` and target Poisson rate `lambda` in tasks/second,
set `poisson_arrival_window_fraction = (N - 1) / (lambda * D)`, where `D` is the
median selected trace span after replay time scaling. The fraction must be finite
and positive; it has no upper limit. Keep the seed, cases and task count fixed
across rate points. Report the realized rate separately: exponential arrivals
do not end at exactly their expected time. A rate sweep needs a new run ID per
point and a failure deadline beyond the realized arrival span plus execution
time; no host apply, image rebuild or prediction retraining is needed.

Prepare the default unique SWE-rebench corpus on the machine holding the raw
recordings, then copy the entire output directory to the experiment host:

```powershell
python -m clawbox.cli experiment import-corpus `
  C:\path\to\recordings --output C:\path\to\replay-corpus --count 332
```

The command converts older recordings to the current replay format, writes
relocatable paths in `workload.json`, and alternates PennyLane and SQLGlot before appending
the remaining projects lexically. Traces whose tool interface cannot be adapted
are skipped without changing their calls; `workload.json` records each skipped
source, hash, and reason, and the command fails if it cannot fill `--count` with
compatible unique traces. Configure `workload.source: swe_rebench`, set
`workload.input` to that manifest, and set `session_assignment: unique`, or use
`clawbox experiment configure BASE --workload-manifest PATH/workload.json ...`;
that option applies all three settings and removes the base spec's inline cases. The
maximum configured concurrency must not exceed the manifest's case count. The
manifest preserves each original source path and SHA-256. It does not guess Tool
template identities: use one Tool template containing every selected repository,
or add a complete case-level `sandbox` for each repository before validation.

On an ARM64 experiment host, build the selected Tool images from the prepared
`/testbed` directories in the original SWE-ReBench images. The host needs a
native ARM64 Docker daemon, the pinned SWE-bench checkout and its Python
dependencies, a built Tool Bridge binary, and an immutable Cube Tool base-image
digest:

```bash
python -m pip install -e '.[images]'
python scripts/build-swe-rebench-arm64.py \
  --dataset /path/to/filtered.parquet \
  --selection /path/to/selection.json --expected-count 539 \
  --swebench-root /path/to/pinned/SWE-bench \
  --registry 127.0.0.1:5000/clawbox \
  --mapping /path/to/task-image-mapping.json \
  --tool-bridge-binary /path/to/tool-bridge \
  --cube-tool-base-image '127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:DIGEST' \
  --workspace-root /path/to/temporary-workspaces \
  --registry-proxy socks5h://127.0.0.1:1080 \
  --workers 4 --min-free-gib 150 --push
```

The command resolves each original image to an immutable digest, downloads its
task layer, verifies the blob digest, and copies its existing Git checkout. It
does not clone or reset the repository. Before starting the task pipelines, it
uses the pinned SWE-bench harness to build the stock `v1` ARM64 base and layers
the shared native build packages from
`docker/Dockerfile.swe-rebench-arm64-common` into a `v2-<recipe-sha256>` image.
The common-base recipe hash also versions environment and instance image tags,
so changing the recipe invalidates cached environments. Architecture-specific
Python dependencies and the editable project install are then rebuilt on the
ARM64 environment layer. The recorded `rasterio 1.4.3` has no PyPI ARM64 wheel
and requires a newer GDAL than the Ubuntu base provides, so the ARM64 recipe
uses the nearest compatible maintenance release, `rasterio 1.3.11`.
`--common-base-dockerfile` selects a different shared base recipe and requires
rebuilding the affected task images.

The default is four concurrent task pipelines; `--workers` changes only
build-time concurrency. `--min-free-gib` is the free-space floor, in GiB,
checked before each download. Temporary compressed layers and workspaces are
removed after each task unless `--keep-workspaces` is set. Failed extractions
are cleaned up, and retries discard incomplete workspaces left by interrupted
processes. Extraction rejects paths and links escaping the checkout and special
device files. Shared Docker environment images remain cached across tasks and
runs; tasks sharing an environment serialize its creation. Repeating the command
reuses supported mapping entries and Docker environment layers. A changed
dataset environment, original image digest, common-base recipe, Tool base image,
bridge, or entrypoint produces a new recipe revision and requires rebuilding
that task image. The mapping records current failures before `--fail-fast`
terminates, so reruns retain the actionable cause and retry failed entries.
The updated recipe version also rebuilds mappings produced before these cache
and extraction checks were introduced.

When the task workspaces and dependencies are already verified and only the
ClawTune guest collector contract/code changed, reuse the immutable task images
instead of running the full ARM64 dependency build again:

Prepare the shared Tool collector image and template with the current ClawTune
checkout selected by `CLAWTUNE_ROOT` in the machine environment:

```bash
bash scripts/clawbox experiment images --role sandbox \
  --profile "$HOME/.config/clawbox/host.json" --tool-prewarm enabled
```

The saved build settings must identify Go and the source/generated headers for
the actual Cube guest kernel. Set `--go`, `--kernel-source`, and `--kernel-build`
on the first build or when those locations change. `--role sandbox` updates
only the Tool image/template; `--role runtime` updates only Runtime. Host-only
ClawBox changes require neither image rebuild.

| Image option | Meaning |
| --- | --- |
| `--tool-prewarm enabled\|disabled` | Default `enabled`. Compile and attach Tool BPF before template readiness, then reuse the restored collector. No measurement window is opened during preparation. |
| `--tool-prewarm-timeout-seconds N` | Default 120 seconds; positive deadline for collector initialization during Tool template preparation. Failure prevents template readiness. |

These options are saved with the image build settings. Changing them requires
new Tool images/templates and a new qualified run ID, but no host apply or
prediction retraining when collector measurement semantics are unchanged.
Runtime image builds also materialize a content-verified prepared predictor
cache from the selected immutable seed. Runtime VMs read the cache from their
shared immutable image layer; it is not copied into each session's private
writable state. A session uses it only when the Python ABI, feature bound, cache
digest, and both private source-snapshot digests match; otherwise startup
rebuilds the same state from JSON. Changing the seed or this ClawTune
predictor/cache code therefore requires rebuilding the single Runtime image and
using a new qualification/run ID. It does not require rebuilding Tool task
images or retraining when the underlying seed and prediction semantics remain
unchanged.
The standalone Runtime disables its local optional eBPF tool monitor because
commands execute in the separate Tool VM. The prewarmed Tool collector remains
the authoritative command-resource measurement path.
The collector refresh below carries the source image's prewarm settings to each
task image. Task workspaces/dependencies are retained. Runtime templates remain
valid for a Tool-only collector change. Validate collection after restore before
refreshing the full workload.

```bash
python scripts/refresh-swe-collector-images.py \
  --workload /absolute/path/workload-ready.json \
  --collector-image registry/clawbox/validated-tool@sha256:DIGEST \
  --clawtune-revision GIT_SHA --node CUBE_NODE \
  --expected-kernel-version KERNEL_ID \
  --output-dir /data/clawbox-environments/collector-refresh \
  --workers 8 --batch-size 16 --minimum-free-gib 64 \
  --storage-check-path /data/CubeMaster/storage
```

The command adds an immutable collector-only OCI layer, registers new Tool
templates, and writes a new workload/catalog without changing the old images or
templates. Completed entries are reused on repeat execution; failed entries are
retried. `--workers` bounds concurrent image/template work and
`--batch-size` bounds the number of tasks allowed to retain intermediate build
state at once (`0`, the default, uses one batch). `--minimum-free-gib` is the
per-filesystem safety floor in GiB. Repeat `--storage-check-path` for any separate
filesystem used by the registry or Cube artifact store; each path must already
exist. On a disk-constrained dedicated builder,
`--prune-build-cache-between-batches` deletes all unused Docker build cache after
each non-final batch; do not use it on a shared builder whose cache must be
preserved. It does not prune images, containers, or volumes. First validate
the collector source image on a real VM. Then use the new workload in a newly
qualified experiment with a new run ID. This update does not rebuild task
dependencies, apply host settings, or retrain an external bootstrap seed.

If a completed entry's immutable image is still valid but its registered Cube
template no longer launches, re-register only that template from the saved
image. Supply the exact workload case ID once per affected template and a
stable generation label:

```bash
python scripts/refresh-swe-collector-images.py \
  [the same workload, collector, node, kernel, output, and storage options] \
  --reregister-case CASE_ID --reregister-generation restore-repair-01
```

Repeat `--reregister-case` for multiple entries and reuse the same generation
when resuming an interruption. Other completed entries are reused. The command
requires the prior completed state, retains the old template, verifies that the
replacement has the same Cube kernel identity, and rewrites the generated
workload/catalog with the new template ID. A different generation deliberately
registers another immutable template. This operation does not rebuild or push
the task image, but the changed template identity requires a new sharing proof,
qualification receipt, and run ID.

Training uses separate successful runs with complete measurements, final task
validation, and verified cleanup. Missing observations remain unavailable; they
are never replaced with zero. Keep the generated JSON and `.kb.json` together.
Changing images, architecture, VM size, collector semantics, or training data
requires retraining.

## Validate, qualify, and run

`launch` is the normal entry point. It validates inputs and host state, reuses a
matching qualification receipt or runs preflight, then starts or resumes the run:

```bash
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/eval.yaml" \
  --run-id eval-01
bash scripts/clawbox experiment status eval-01
bash scripts/clawbox experiment report eval-01
```

Normal `launch` reuses each case's saved `metadata.input_validation` identity,
checks that referenced files still exist, loads the selected prediction seed once,
and lets qualification execute the first real Tool call in every environment. It
does not reparse every immutable trace or resolve every recorded command before
starting. Run `experiment validate SPEC --inputs` only when you explicitly want a
full byte-level audit after changing or replacing workload/seed files; that command
rehashes and reparses the inputs and can take several minutes for a large corpus.

Useful commands:

| Command | Purpose |
| --- | --- |
| `trace`, `validate --inputs`, `describe`, `plan` | Read-only inspection; no VMs |
| `doctor SPEC --probe-vm` | Check services, templates, resources, revisions, and optionally one real VM |
| `prove-template-sharing SPEC --output FILE` | Check physical Base sharing once for every exact Tool template and freeze reusable evidence |
| `qualify SPEC` | Run preflight separately and write `SPEC.qualification.json` |
| `launch SPEC --run-id ID` | Preferred validate/qualify/run/resume workflow |
| `run SPEC`, `resume ID` | Lower-level execution requiring a valid qualification receipt |
| `status`, `report`, `collect` | Inspect progress or regenerate reports without rerunning work |
| `abort`, `destroy` | Stop owned VMs or clean them up while retaining result files |

When `launch` receives an explicit `--qualification PATH`, that receipt must be
reusable for the specification or the command fails before starting a new
qualification. Omit the option to let `launch` create the default receipt, or
use `--force-qualify` when replacing the explicit receipt is intentional.

Normal session finalization shares an eight-request delete limit within each
worker. `abort` and `destroy` issue at most 8 concurrent Cube destroy requests and wait
180 seconds by default. For a dense interrupted run, set
`--cleanup-workers N` and `--cleanup-timeout-seconds S`; these change only the
bounded cleanup operation, in requests and seconds respectively. They do not
rebuild images, retrain predictions, alter the frozen experiment, or require a
new run ID. Cleanup still exits nonzero unless a fresh Cube inventory proves
that no VM owned by the run remains.

A qualification receipt is bound to the maintained capability-contract version,
templates/images, VM shapes, nodes/resources, policies, case coverage, prediction
seed, and saved input identities. Run IDs, experiment IDs, formal offered
concurrency, arrival timing, reporting, documentation, and CLI-only changes do not
invalidate it because the smoke does not prove those properties. Changes to VM
execution, ToolBridge/policy handling, prediction/measurement integration, or the
qualification itself bump the receipt schema and require one new qualification.
A lower qualification
concurrency bounds the qualification worker pool while one session still executes
every workload case. By default, `launch` uses the smallest of the target
concurrency, `execution.sandbox_create_concurrency`, and the workload case count;
use `--qualification-concurrency N` only when a different smoke-test bound is
needed. The receipt records both the smoke bound and target concurrency for audit;
the target value is not a reuse gate. This
capability smoke does not prove full-density performance. After interruption, restore
the host, retain the result directory, and launch the frozen spec with the same
run ID. Use a new run ID for any configuration change.

Template-aware placement additionally proves that two VMs from each Tool
template physically share Cube's file-backed RAM Base. For a heterogeneous
corpus, create that evidence once instead of recreating two probe VMs per
template during every qualification:

```bash
bash scripts/clawbox --sudo experiment prove-template-sharing eval.yaml \
  --output /data/clawbox-evidence/template-sharing.json \
  --artifact-root /data/clawbox-evidence/template-sharing-raw
bash scripts/clawbox experiment configure base.yaml eval-with-proof.yaml \
  --template-base-sharing-artifact /data/clawbox-evidence/template-sharing.json
```

If Cube reports an explicit failed-create transport error, retain that raw
attempt and resume into a new artifact root. Repeat `--resume-result` for each
retained `result.json`; mappings and cleanup are revalidated, their file hashes
are bound into the final proof, and only templates without valid evidence are
created again:

```bash
bash scripts/clawbox --sudo experiment prove-template-sharing eval.yaml \
  --resume-result /data/clawbox-evidence/failed-attempt/result.json \
  --output /data/clawbox-evidence/template-sharing.json \
  --artifact-root /data/clawbox-evidence/template-sharing-resumed-raw
```

In editable schema-4 YAML the corresponding field is
`evidence.template_base_sharing_artifact`; schema 3 uses
`resources.template_base_sharing_artifact`. Reuse verifies the exact template
IDs, image digests, VM sizes and architecture, target node, and the retained
raw proof hashes. A changed Tool template/image, VM shape, target node, the
CubeSandbox template-RAM mapping mechanism, or missing raw proof requires
running `prove-template-sharing` again. Ordinary ClawBox, ClawTune, Cubelet
placement, or Cube configuration updates do not invalidate the physical
mapping evidence. Changing the artifact
path requires a new run ID and qualification receipt, but not an image rebuild.
This evidence skips only the repeated sharing probe; it never substitutes for
the experiment's own VM creation or full-density measurement.

## Reproduce the shared-cache CPU-placement experiment

The repository includes frozen specifications, traces, and prediction data for
a SQLGlot comparison between full-pool (`c0`) and one-cache-cluster (`c2`) CPU
placement. On a compatible ARM64 host, run both comparison orders with:

```bash
bash scripts/run-llc-shared-reproduction.sh
```

The command checks the frozen files, VM image digests, memory settings, and CPU
topology before it starts VMs. It qualifies and runs each order separately, then
prints both reports.

Check compatibility and generated inputs without starting a VM:

```bash
bash scripts/run-llc-shared-reproduction.sh --prepare-only
```

Prerequisites are the normal installation and applied host profile plus the
original Runtime and Tool images registered under their recorded digests. VM
images and CubeSandbox host installation are not stored in Git; the command
fails with the exact missing or mismatched prerequisite instead of substituting
a different image or resource setting.

## Read results

| Run path | Contents |
| --- | --- |
| `run-state.json` | Supervisor state, active trial, ownership, and cleanup status |
| `experiment.yaml`, `qualification.json` | Frozen input and preflight evidence |
| `arms/*.json`, `arms/*.complete` | Raw arm result and verified completion marker |
| `summary.json`, `summary.csv`, `report.md` | Aggregated results |
| `memory-timeseries.csv` | Sampled cgroup and NUMA memory data |
| `attempts/ATTEMPT/ARM/` | Events, logs, telemetry, gateway data, and VM ownership |

Before comparing performance, check submitted and completed sessions, task
validation, failures, OOMs, timeouts, duplicate execution IDs, telemetry joins,
and cleanup. Compare only arms with the same workload, hardware scope, case mix,
arrival schedule, VM resources, and evidence. Configured capacity, measured
residency, predictions, and admission reservations are different quantities.

Throughput and completion latency are primary outcomes. Admission wait,
placement wait, tool time, CPU pressure, memory residency, checkpoint cost, and
prediction error explain the result but do not replace it. Empty or `null`
measurements are unavailable, not zero. NUMA allocation-event counters are pages,
not cross-NUMA traffic bytes; do not infer traffic without a qualified host PMU.

`offered_to_final_response_*` measures scheduled arrival to host receipt of the final response, including completion polling and transport but excluding later artifact collection, validation, and cleanup; `offered_to_completion_*` retains the older postcollection endpoint.

For startup delays, compare `native_ssh_warmup_finished.executor_queue_seconds`
with `warmup_execution_seconds`. The startup `native_ssh_identity_ready` event
reports the session's blocking join separately as `critical_path_wait_seconds`.
These intervals overlap Runtime startup; do not add them to sidecar readiness.
For per-tool delays, `tool_admitted.memory_baseline_timing` distinguishes
admission-lock waits from the kernel memory sample and reports calling-thread
CPU time alongside wall time.
Host-side timing and lock fixes require a new run ID, without rebuilding guest
images or retraining predictions when their contents and semantics are unchanged.

## When changes take effect

| Change | Required action |
| --- | --- |
| Mechanism values, concurrency, timeouts, workload, or evidence path | Validate, qualify, and use a new run ID |
| CPU/NUMA topology, capacities, mounts, host networking, or TAP pool | `host check`, `host apply`, reconfigure, qualify, and use a new run ID |
| VM vCPU, RAM, disk, image, kernel, workspace, or guest dependency | Rebuild/import as needed, register new templates, apply host config, and qualify |
| Prediction environment, collector semantics, or training observations | Retrain and use new immutable prediction artifacts |
| Reboot with unchanged configuration | Reapply the saved host YAML on an idle host, then resume the same frozen run |
