# Run an experiment on a new host

Run these commands in a terminal on an ARM64 Linux/KVM host. The supported
interface is `clawbox experiment`; no coding assistant is needed. Start
with two concurrent sessions so both compute nodes are exercised, then move
to a measured workload.

## 1. Install and inspect the host

Complete [installation steps 1–4](installation.md) first. They install the
patched standalone CubeSandbox and matching Python SDK, import guest images
and a guest kernel, and register the agent and tool VM templates. These
artifacts are not distributed with this repository; cloning it and
installing the Python package alone cannot create working VMs.

Mount the intended storage devices before configuring CubeSandbox. Select
the Cubelet data location in the installer's `.env` file. The ClawBox host
configuration does not format disks, move existing Cubelet data, or change
storage credentials. Check the actual disks and NUMA layout:

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
df -hT /data /data/cubelet
numactl --hardware
```

Run subsequent commands from the ClawBox checkout. In each new shell, load
the Python environment and the machine settings created during installation:

```bash
cd "$HOME/src/ClawBox"  # Use your checkout path.
source .venv/bin/activate
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
sudo -v
clawbox experiment host inspect
```

`host inspect` lists NUMA nodes, CPU lists, memory, and distances.
Its `free_mib` value excludes reclaimable cache; it is neither the host's
available-memory figure nor a safe capacity to allocate in full.

## 2. Configure the compute nodes and shared pool

Create an editable host configuration and open it:

```bash
clawbox experiment host init "$HOME/.config/clawbox/host.yaml"
${EDITOR:-vi} "$HOME/.config/clawbox/host.yaml"
```

For an existing installation, you can instead initialize from its applied
`host.json` profile. Use this *instead of* the first `host init` command;
the destination file must not already exist:

```bash
clawbox experiment host init "$HOME/.config/clawbox/host.yaml" \
  --from-profile "$HOME/.config/clawbox/host.json"
```

Fill in `runtime_template`, `tool_template`, `node`, and
`cube_source`, and review every directory. `node` is the CubeSandbox
node where both templates have READY replicas; it is not a NUMA number.
For an imported profile, verify `cold_root` against the installed
snapshot directory.

A new configuration uses NUMA 0 and 1 for compute and NUMA 2 for the
shared pool. Use `host inspect` to adapt the IDs, CPU lists, and capacities
to the actual machine. When converting a single-node profile, remove its
top-level `local_node`, `local_gib`, `low_gib`, and `high_gib` fields
and use this structure:

```yaml
compute_nodes:
  - node_id: node0
    numa_node: 0
    cpus: null
    memory_gib: 36
    low_gib: 28
    high_gib: 32
  - node_id: node1
    numa_node: 1
    cpus: null
    memory_gib: 36
    low_gib: 28
    high_gib: 32
warm: true
warm_node: 2
warm_gib: 128
shared_borrow_percent: 50
warm_root: /mnt/clawbox-pool
```

`cpus: null` selects all CPUs on that NUMA node. To use a subset,
provide a CPU-list string such as `"0-39"`; `host check` rejects CPUs
outside the node and overlapping CPU lists. Each compute node may have
different capacity and watermarks. The example has 36 GiB of local
capacity per compute node and one 128 GiB shared pool. At most 50% of the
pool, or 64 GiB, is reserved for running VMs across both nodes.

| Host YAML field | Meaning |
| --- | --- |
| `compute_nodes[].numa_node`, `cpus` | NUMA node and allowed CPUs for each compute node |
| `compute_nodes[].memory_gib` | Local memory capacity used by that node's controller, in GiB |
| `compute_nodes[].low_gib`, `high_gib` | Per-node reclaim target and high watermark; require `0 < low < high < capacity` |
| `warm_node`, `warm_gib` | Shared-pool NUMA node and capacity; it must differ from the compute nodes |
| `shared_borrow_percent` | Maximum share of the pool that running VMs may reserve, from 0 to 50 |
| `warm_root` | Dedicated tmpfs mount path; use an empty directory outside other data roots |
| `cold_root` | Disk-backed snapshot path; `warm-only` runs do not spill there |
| `cubelet_data_root` | Path whose Cubelet disk space is checked; it does not relocate Cubelet data |
| `output_root` | Path whose result-disk space is checked; pass it through `--output-root` when running |
| `minimum_disk_free_gib` | Minimum free space on each checked data filesystem; usage must also stay below 85% |

Set `CLAWBOX_WARM_ROOT`, `CLAWBOX_COLD_ROOT`, and
`CLAWBOX_OUTPUT_ROOT` in `machine.env` to the corresponding
`warm_root`, `cold_root`, and `output_root` values in the host YAML.
Reload it, check prerequisites, and apply the configuration while the VM
pool is idle:

```bash
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
clawbox experiment host apply "$HOME/.config/clawbox/host.yaml"
```

`host check` reports `ready_for_apply: true` on success without
creating VMs or changing host settings. The report supplies a remedy for
each failed check. `host apply` checks again, configures memory and
snapshot storage, updates CubeSandbox service settings when needed, and
creates and destroys a probe VM. It saves
`~/.config/clawbox/host.json` only after the probe succeeds. If it
fails after partially applying host settings, correct the reported cause
and run `host apply` again on an idle VM pool.

After changing NUMA nodes, capacities, CPU lists, watermarks, or paths,
repeat `host check` and `host apply`, then generate a new experiment
YAML. If changing the NUMA node of an existing tmpfs mount, preserve
needed snapshots and have the old mount unmounted while the VM pool is
idle, or choose a new empty path. Reapply the saved host YAML after a
reboot. For the exact placement and memory limits, see
[Host and memory configuration](supernode.md).

## 3. Run the starter workload

Use the result directory checked by the host configuration. The
installation procedure creates it:

```bash
test -w "$CLAWBOX_OUTPUT_ROOT"
mkdir -p "$HOME/clawbox-specs"
clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/smoke.yaml" \
  --trace "$PWD/examples/traces/smoke.jsonl" \
  --experiment-id first-smoke --concurrency 2
clawbox experiment validate "$HOME/clawbox-specs/smoke.yaml" --inputs
clawbox experiment describe "$HOME/clawbox-specs/smoke.yaml"
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/smoke.yaml" --run-id smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment status smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment report smoke-01
```

`configure` copies the templates, image digests, NUMA layout, and
capacity from the applied host profile. With the example layout, the
total admission budget is the sum of both 32 GiB high watermarks, while
each node makes its own admission decisions. `configure` will not
overwrite an existing output YAML unless given `--force`. Use a new
run ID for a changed configuration.

`launch` checks inputs and host readiness, tests VM execution at the
required concurrency, then runs the workload. Look for final
`state: succeeded`, successful task validation, and verified cleanup.
The starter task writes `complete` to `/workspace/result.txt` through
the agent and tool VMs. It checks the execution path; it is not a
high-pressure performance result. Repeating `launch` with the same YAML
and run ID returns the active run's status or its completed report, or
resumes an interrupted run. Add `--detach` for a long run and use
`status` to monitor it.

To stop and clean up a run that you own:

```bash
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment abort smoke-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment destroy smoke-01
```

## 4. Train a prediction file and compare policies

The next trace touches 64 MiB for three seconds so guest memory
sampling has a measurable interval. The short `smoke.jsonl` command
is not suitable for memory training. Missing memory observations
cannot be replaced with zeroes.

```bash
clawbox experiment configure examples/experiments/getting-started.yaml \
  "$HOME/clawbox-specs/memory-train.yaml" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" \
  --prompt 'Allocate and touch 64 MiB for three seconds, then write complete to /workspace/result.txt.' \
  --repository self-service/memory --experiment-id memory-train \
  --baseline tool-static-resident --concurrency 2
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/memory-train.yaml" --run-id memory-train-01
clawbox experiment train "$CLAWBOX_OUTPUT_ROOT/memory-train-01" \
  --trace "$PWD/examples/traces/memory-smoke.jsonl" --repository self-service/memory \
  --output "$HOME/clawbox-specs/memory-p50.json"
clawbox experiment configure "$HOME/clawbox-specs/memory-train.yaml" \
  "$HOME/clawbox-specs/memory-eval.yaml" --experiment-id memory-eval \
  --baseline tool-static-resident --baseline tool-p50-resident \
  --baseline tool-p50-wait-reactive \
  --prediction-artifact "$HOME/clawbox-specs/memory-p50.json" \
  --static-tool-memory-mib auto --model-wait-prediction-seconds auto \
  --snapshot-storage warm-only
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  "$HOME/clawbox-specs/memory-eval.yaml" --run-id memory-eval-01
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment report memory-eval-01
```

This walkthrough requires `warm: true`. A single repetition checks
the training and prediction path, not performance differences. A light
workload may never cross a high watermark and therefore may not pause a
VM during the formal run; the preflight run still checks snapshot,
restoration, and shared-pool borrowing. For a real study, train on a
separate, successful run with complete measurements, use multiple
repetitions, and retain failures. The tool VM image must contain the
task repository, dependencies, and intended starting version; editing
a trace does not prepare the guest environment.

Use `clawbox experiment configure --help` for all overrides and
`clawbox experiment baselines` for the supported policy names.

| To change | Use |
| --- | --- |
| Concurrency and repetitions | `--concurrency 1,4,8 --repetitions 3` |
| Admission budget and safety margins | `--pool-memory-gib`, `--checkpoint-headroom-gib`, `--emergency-free-memory-gib` |
| NUMA nodes, allowed CPUs, local capacities, or shared-pool size | Edit the host YAML, run `host check` and `host apply`, then `configure` |
| VM vCPUs or guest RAM | Register a new template with the desired `--cpu-millicores` and `--memory-mib`, update the host YAML, then apply it |
| VM writable disk | Set `--writable-layer-size` when registering the template |
| Cubelet data disk | Select its location during CubeSandbox installation; move existing data separately before updating `cubelet_data_root` |
| Snapshot mechanism and storage | `--snapshot-mechanism full-copy` or `incremental-cow`; `--snapshot-storage warm-only` or `tiered` |
| Timeouts, sampling interval, and random seed | `--command-timeout-seconds`, `--arm-timeout-seconds`, `--memory-sample-interval-seconds`, `--random-seed` |
| Predictions and command memory reservation | `--prediction-artifact`, `--static-tool-memory-mib`, `--model-wait-prediction-seconds` |

You can edit fields without a CLI option in the generated YAML; see the
[configuration reference](guide.md). After a change, run `validate
--inputs`, `describe`, and `launch` again with a new run ID. Keep VM
configured RAM, admission reservations, measured memory use, and snapshot
storage separate when reading the report.

## 5. Resolve common failures

| Result | Next step |
| --- | --- |
| `host check` fails | Follow that check's `remedy`, then repeat `host check` before applying |
| VM pool is busy | Check `status RUN_ID`; wait or use `abort`/`destroy` for a run you own |
| Template has no READY replica | Check its registered CubeSandbox node and template ID; the node name is not a NUMA number |
| Snapshot storage reports `no more resource` | Check Cubelet disk usage and the S3lvol backend if enabled; see [installation](installation.md) |
| ClawTune revision differs from the images | Update both guest images with `clawbox experiment images`, then reconfigure the experiment |
| Shared-pool tmpfs has the wrong NUMA policy | Check `findmnt -M WARM_PATH`; preserve snapshots before changing the mount |
| Guest telemetry fails | Check guest kernel headers against the running guest kernel, then rebuild and register the tool image |
| SSH disconnects or a run is interrupted | Restore host settings, then `launch` the same YAML and run ID; keep the result directory |

Logs, `run-state.json`, and per-trial results remain in the run
directory. Do not delete ownership files or treat a failed command as
permission to proceed to the next step.
