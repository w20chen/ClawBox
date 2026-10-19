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
| Experiment YAML | Workload, VM identities, evidence, mechanisms, comparison arms, concurrency, and timeouts |
| `RUN/experiment.yaml` | Frozen specification for one run ID; never edit it in place |

Host capacities use GiB. Experiment VM memory, reservations, and watermarks use
MiB. Result names ending in `_bytes` use bytes. Relative paths resolve from the
current directory; use absolute paths for traces, prediction files, and results.

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

Check without mutation, then apply on an idle host:

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply "$HOME/.config/clawbox/host.yaml"
```

Apply again after reboot or after changing topology, CPU lists, memory capacity,
mounts, or host networking. `host apply` writes the adjacent `host.json` only
after service, storage, and real-VM probes succeed.

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

The schema has six mechanism dimensions plus deployment and coordination:

| Mechanism | Main choices | What it controls |
| --- | --- | --- |
| `admission` | `off`, `fixed`, `predicted` | Whether a tool command may start; reservations do not resize guest RAM |
| `runtime` | memory protection, CPU release boost, proactive reclaim | Per-VM reaction to measured host contention |
| `placement` | round-robin, explicit, OS default, load-aware, optional C2 | Session NUMA placement and eligible tool-call CPU affinity |
| `idle_reclaim` | `off`, `wait`; tool or agent-and-tool scope | Checkpointing while a real model request is outstanding |
| `shared_memory` | `off`, `hard`, `pressure` | Temporary placement of running VM memory in the shared NUMA pool |
| `checkpoint` | full-copy or incremental CoW; disk, WARM, or tiered | Snapshot encoding, storage, and residency |
| `deployment` | `managed`, `os-default` | Managed lifecycle accounting versus the guarded OS baseline |
| `coordination` | `reclaim_first`, `measured_cost` | Choice between safe reclaim and borrowing actions |

Use `configure --fixed-methods --recipe FAMILY` for a controlled single-family
comparison. It resets unrelated mechanism switches while retaining the workload,
host resources, measurements, and numeric tuning. Inspect `describe` because arm
names and recipe labels never select runtime behavior. Fixed-method comparisons reset
tool-call CPU placement to C0. The integrated recipe also uses C0 for its two
control arms while preserving the configured design's CPU policy.

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

### Resident capacity baselines

`--recipe resident-capacity` generates three supported arms:

| Arm | Charge per VM |
| --- | --- |
| `resident-capacity-1` | configured RAM * 1.0 |
| `resident-capacity-0-8` | configured RAM * 0.8 |
| `resident-capacity-0-75` | configured RAM * 0.75 |

These arms keep both VMs resident, disable runtime checkpointing, tool-call
admission, runtime optimizations, shared-memory borrowing and template affinity,
and use round-robin placement with C0 CPU scheduling. All generated recipes currently set
`placement.placement_cpu_overcommit` to 1024, also the global default, keeping the
CPU slot mechanism available but making it unlikely to limit these experiments. This does not
change the allowed host CPU lists or VM vCPU counts. Edit the arm override to
choose another multiplier. VM RAM is unchanged.
Each VM's charge is rounded up separately to MiB; a session reserves the sum of
its Agent and Tool charges until cleanup, including model waits. For example,
a 6144 MiB VM is charged 4916 MiB at 0.8 or 4608 MiB at 0.75. Failed creation
releases the charge during cleanup. Admission uses only the persistent nominal
ledger: existing charges plus the new pair's charge must fit the configured
per-domain budget. For this mode the budget equals each compute node's
`memory_capacity_mib`; the aggregate budget is the sum of those capacities.
With no compute nodes, it uses `local_memory_capacity_mib` when present, otherwise
`pool_memory_budget_mib`. LOW/HIGH remain host-profile fields for measured modes
but do not determine the pure-ledger budget. RSS, in-flight physical growth, free-memory floors, congestion
and LOW/HIGH hysteresis do not veto admission in this mode. Memory samples remain
available as observations; VM creation and kernel memory failures remain real
experiment outcomes.

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

For custom arms, the explicit fields are
`deployment.resident_capacity_accounting: true` and
`deployment.lifecycle_reservation_scale` (greater than zero and at most one).
Discounting requires managed resident capacity accounting. Pure ledger mode
requires tool admission off and rejects shared-memory borrowing and proactive
memory reclaim, whose execution paths are incompatible with this mode. Existing experiments
retain their original accounting unless this mode is enabled. Results include
`resource_scope.session_capacity_charge_mib` and the scale for auditing.

### C0 and C2 tool-call CPU placement

`mechanisms.placement.tool_call_cpu_policy: c0` keeps the Tool VM on the full
selected-NUMA CPU mask and leaves scheduling to Linux. `c2` applies only to
`agent-tool` `exec` calls:

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

C2 requires readable Linux cache/cluster/SMT topology and at least two eligible
LLC clusters in the selected NUMA CPU pool. Filesystem calls, backend maintenance,
unknown operations, and short `exec` calls retain C0 behavior. Compare C0 and C2
with identical VM resources, CPU pools, workload, admission, and memory policy.

### Workloads and replay

The supported path uses `agent.driver: openclaw` and replay inference. A replay
trace contains paired schema-6 `span_start` and `span_end` records. Model
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

A qualification receipt is bound to the exact spec, implementation, templates,
images, nodes, concurrency, and referenced file hashes. A lower qualification
concurrency does not prove full-density performance. After interruption, restore
the host, retain the result directory, and launch the frozen spec with the same
run ID. Use a new run ID for any configuration change.

## Reproduce the shared-LLC C0/C2 experiment

This repository includes the frozen specifications, traces, and prediction
artifact from the 2026-10-19 SQLGlot experiment. On a configured compatible
ARM64 host, reproduce both arm orders with one command:

```bash
bash scripts/run-llc-shared-reproduction.sh
```

The command verifies every frozen asset by SHA-256, verifies the original VM
image digests and memory configuration in the applied host profile, selects two
8-logical-CPU LLC clusters on each of two NUMA nodes, and runs the 16-session
C0/C2 comparison sequentially in both arm orders. It forces a fresh real-VM
qualification for each order and prints both reports. The reference runs measured
`+4.12%` and `+5.95%`; the mean C2 throughput gain was `+5.03%`.

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
