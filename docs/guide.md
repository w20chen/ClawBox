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

The installed CubeSandbox SDK must include `sdk-control-timeouts.patch` and
`sdk-lifecycle-timeouts.patch`. Its request timeout defaults to 30 seconds per
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
| `shared_borrow_percent` | Maximum live-VM share of the common pool, from 0 to 50 |
| `warm_root`, `cold_root`, `output_root` | Dedicated WARM tmpfs, COLD snapshot, and result paths |
| `minimum_disk_free_gib` | Required free space; CubeSandbox also rejects snapshot writes above 85% filesystem use |
| `tcp_tw_reuse` | `null` preserves the host value; `0`, `1`, and `2` request Linux's documented modes |
| `cubelet_tap_init_num` | Optional pre-created TAP count; cover both VMs for every simultaneous session |
| `cube_scheduler_overcommit` | Optional map from Cube node `InstanceType` to positive `cpu_ratio` and `mem_ratio`; `null` preserves Cube's settings. These multiply Cube's reported configured-VM quotas, not physical CPU/RAM allocations. |
| `cube_scheduler_vm_count_ratio` | Optional positive per-instance multiplier over Cube's node-reported VM-count limit; `null` preserves Cube's setting. This is a scheduler limit, not physical capacity. |

Check without mutation, then apply on an idle host:

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply "$HOME/.config/clawbox/host.yaml"
```

Apply again after reboot or after changing topology, CPU lists, memory capacity,
mounts, or host networking. `host apply` writes the adjacent `host.json` only
after service, storage, and real-VM probes succeed.

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

For predicted admission, `--prediction-mode runtime` uses an immutable trained
knowledge base (KB) for the matching repository and Tool image.
`--prediction-mode cold-start` uses one immutable ClawTune bootstrap KB for
every workload repository; its
artifact must declare `prediction_scope: clawtune_cold_start` and
`repository: "*"`. The cold-start mode still verifies the KB hash, validated
host-memory calibration, and the complete Tool environment identity, including
VM size. The bootstrap KB must contain compatible memory observations; a
time-only seed cannot support predicted memory admission. Changing the mode or
artifact requires a new run ID, not an image rebuild.

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
created at once (default `8`; unit: session pairs). It does not limit tool calls.
Set it no higher than the largest `execution.concurrency_levels` value. Changing
it requires validation, qualification, and a new run ID.

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

Useful commands:

| Command | Purpose |
| --- | --- |
| `trace`, `validate --inputs`, `describe`, `plan` | Read-only inspection; no VMs |
| `doctor SPEC --probe-vm` | Check services, templates, resources, revisions, and optionally one real VM |
| `qualify SPEC` | Run preflight separately and write `SPEC.qualification.json` |
| `launch SPEC --run-id ID` | Preferred validate/qualify/run/resume workflow |
| `run SPEC`, `resume ID` | Lower-level execution requiring a valid qualification receipt |
| `status`, `report`, `collect` | Inspect progress or regenerate reports without rerunning work |
| `abort`, `destroy` | Stop owned VMs or clean them up while retaining result files |

`abort` and `destroy` issue at most 8 concurrent Cube destroy requests and wait
180 seconds by default. For a dense interrupted run, set
`--cleanup-workers N` and `--cleanup-timeout-seconds S`; these change only the
bounded cleanup operation, in requests and seconds respectively. They do not
rebuild images, retrain predictions, alter the frozen experiment, or require a
new run ID. Cleanup still exits nonzero unless a fresh Cube inventory proves
that no VM owned by the run remains.

A qualification receipt is bound to the exact spec, implementation, templates,
images, nodes, concurrency, and referenced file hashes. A lower qualification
concurrency does not prove full-density performance. After interruption, restore
the host, retain the result directory, and launch the frozen spec with the same
run ID. Use a new run ID for any configuration change.

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

## When changes take effect

| Change | Required action |
| --- | --- |
| Mechanism values, concurrency, timeouts, workload, or evidence path | Validate, qualify, and use a new run ID |
| CPU/NUMA topology, capacities, mounts, host networking, or TAP pool | `host check`, `host apply`, reconfigure, qualify, and use a new run ID |
| VM vCPU, RAM, disk, image, kernel, workspace, or guest dependency | Rebuild/import as needed, register new templates, apply host config, and qualify |
| Prediction environment, collector semantics, or training observations | Retrain and use new immutable prediction artifacts |
| Reboot with unchanged configuration | Reapply the saved host YAML on an idle host, then resume the same frozen run |
