# Baseline guide

A baseline selects two independent behaviors:

1. how much memory ClawBox reserves before a Tool command starts;
2. whether idle virtual machines stay in memory or are checkpointed.

In supported managed OpenClaw experiments, switching a baseline does not change
the Runtime VM, Tool VM, OpenClaw process, native SSH path, workspace, or
telemetry. List the exact policy choices implemented by the installed checkout
with:

```bash
clawbox experiment baselines
```

## Resource example

The checked-in c40 YAML uses the following CPU and memory sizes. The disk row
shows the template-registration recipe documented in the experiment guide;
disk size is not stored in the experiment YAML and must also be verified from
the selected CubeSandbox templates.

| Resource | Runtime VM | Tool VM | One agent |
| --- | ---: | ---: | ---: |
| Virtual CPUs | 2 | 2 | 4 |
| Configured memory | 2 GiB | 4 GiB | 6 GiB |
| Writable disk | 20 GiB | 40 GiB | 60 GiB |

The totals are configured capacity, not measured host usage:

| Agents | VMs | Runtime memory | Tool memory | Total VM memory |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 2 GiB | 4 GiB | 6 GiB |
| 5 | 10 | 10 GiB | 20 GiB | 30 GiB |
| 20 | 40 | 40 GiB | 80 GiB | 120 GiB |
| 40 | 80 | 80 GiB | 160 GiB | 240 GiB |
| 60 | 120 | 120 GiB | 240 GiB | 360 GiB |

At c40, the 160 GiB LOCAL budget gives a configured-memory ratio of:

```text
40 * (2048 MiB + 4096 MiB) / 163840 MiB = 1.5x
```

This is overcommit against the configured ClawBox pool. The Kunpeng host may
have more physical memory. Host memory samples in the result show how much was
actually resident.

Writable-disk size belongs to the immutable CubeSandbox template. If the
documented 20 GiB Runtime and 40 GiB Tool templates are used, c40 nominally
offers 2400 GiB (`40 * (20 + 40) GiB`), not necessarily fully allocated
storage. Changing disk size requires a new template.

## Memory checks shared by all baselines

Before creating or restoring a VM, or starting a Tool command, ClawBox checks:

- current host memory use;
- existing reservations;
- the new operation's reservation;
- checkpoint/restore headroom;
- the configured minimum host free memory.

The check completes before SSH starts. The Tool-command reservation is released
after SSH exits and its completion data has been collected. The host free-memory
limit is the same emergency guard for every comparison.

## Available baselines

The names below are accepted by `experiment configure`.

| Baseline name | Command-memory reservation | VM behavior during model waits | Required setting |
| --- | --- | --- | --- |
| `lifetime-full-resident` | Full Runtime + Tool memory for the complete agent lifetime | Both VMs remain running | none |
| `tool-full-resident` | Fixed full Tool amount for each active command | Both VMs remain running | `full_tool_memory_mib` |
| `tool-static-resident` | One fixed amount for each active command | Both VMs remain running | `static_tool_memory_mib` |
| `tool-p90-resident` | ClawTune command-specific P90 prediction | Both VMs remain running | `p90_predictions` |
| `tool-oracle-resident` | Held-out measured demand | Both VMs remain running | `oracle_measurements` |
| `tool-static-eager-reactive` | Fixed amount | Checkpoint at the start of an eligible model wait | `static_tool_memory_mib` |
| `tool-p90-eager-reactive` | Command-specific P90 | Checkpoint at the start of an eligible model wait | `p90_predictions` |
| `tool-p90-fixed-reactive` | Command-specific P90 | Wait 0.5 s by default, then checkpoint | `p90_predictions` |
| `tool-p90-wait-reactive` | Command-specific P90 | Checkpoint only when predicted wait and memory pressure justify it | `p90_predictions` and a request-time model-wait estimate with source |
| `tool-p90-wait-proactive` | Command-specific P90 | Same decision, but restore Runtime before the predicted response time | P90 file, model-wait estimate/source, and prefetch lead |
| `tool-static-time-oracle-reactive` | Fixed amount | Replay-only oracle: checkpoint immediately only when the held-out actual model wait is at least 4.0 s by default | `static_tool_memory_mib` and replay inference |
| `tool-p90-tiered-lru-oracle-reactive` | Command-specific P90 | LOCAL/WARM/COLD hierarchy with LRU selection under pressure | P90 file, replay inference, WARM/COLD paths and capacities |
| `tool-p90-tiered-time-oracle-reactive` | Command-specific P90 | LOCAL/WARM/COLD hierarchy with held-out wait duration guiding placement | P90 file, replay inference, WARM/COLD paths and capacities |

The two tiered policies use the [NUMA memory-pool approximation](tiered-memory-simulation.md).
The c40 study disables WARM for the other eleven policies.

Compatibility names still appear with `clawbox experiment baselines --all` so
old result files can be read. Do not use them for new experiments.

## What the reservation choices mean

`lifetime_full` reserves the configured Runtime and Tool memory before the VM
pair is created and keeps that capacity claim until the agent ends. It is the
most conservative capacity baseline.

`tool_full` reserves `full_tool_memory_mib` for each active Tool command. The
field is required by the experiment schema; set it to the Tool VM's configured
memory when the comparison is intended to represent a full Tool allocation.

`tool_static` reserves `static_tool_memory_mib` for every Tool command,
regardless of the command. The field is required. It provides a simple fixed
estimate to compare with command-specific prediction.

`tool_p90` uses the frozen ClawTune prediction selected by the command's
normalized key. A managed OpenClaw command must provide matching prediction
metadata before it can be admitted. Keep the prediction file and use the same
file for every compared variant.

Native file tools (`read`, `write`, `edit`, and `apply_patch`, routed as
`filesystem`) have no shell-command KB entry. P90 and oracle arms use
`static_tool_memory_mib` for these operations and record the source as
`filesystem_static`. Backend maintenance also uses an explicit static budget.
These reservations must not be described as command-specific P90 estimates.

`tool_oracle` reads held-out measurements and is allowed only with replay. It
is an evaluation upper bound, not a deployable policy and not training data for
the same test workload.

## What the VM choices mean

`resident` leaves both VMs running for the agent's lifetime.

With `snapshot_pause`, ClawBox waits until the Tool has no active SSH command,
then checkpoints the Tool VM and Runtime VM. The Runtime VM is restored before
the pending model response is delivered. The Tool VM stays checkpointed until
the next Tool request, when it is restored and its current SSH endpoint is
resolved again.

The checkpoint decision can be:

- `eager`: start at the beginning of an eligible model wait;
- `fixed_delay`: wait for `fixed_delay_seconds` first;
- `wait_aware_pressure`: checkpoint only when a request-time wait estimate is
  available and the memory-pressure check is true.
- `time_oracle`: evaluation-only. Read the held-out model-wait duration from the
  replay trace and checkpoint only when it is at least
  `checkpoint_break_even_seconds` (4.0 s in the catalog baseline). This is an
  upper-bound baseline and cannot be used with live API inference.

With reactive restore, Runtime restoration begins when the model response is
ready. With proactive restore, it starts `prefetch_lead_seconds` before the
predicted response time. Proactive restore applies to Runtime; Tool restoration
still happens when the next Tool command is admitted.

## Configure a comparison

Repeat `--baseline` to place several baselines in one experiment file:

```bash
clawbox experiment configure \
  examples/experiments/tiered-oracle-rec-a-c40.yaml \
  /data/clawbox-specs/comparison.yaml \
  --concurrency 40 \
  --pool-memory-gib 160 \
  --baseline lifetime-full-resident \
  --baseline tool-static-resident \
  --baseline tool-static-eager-reactive

clawbox experiment describe /data/clawbox-specs/comparison.yaml
```

Change `runtime.memory_mib`, `sandbox.memory_mib`, and the memory pool only when
those values are the intended experiment variables. The YAML VM sizes must
match the selected templates. For all other comparisons, keep workload,
templates, replay timing, arrival schedule, random seed, memory pool, and host
scope unchanged.

See the [experiment guide](experiment-operations.md) for configuration and run
commands, and the [results guide](results-guide.md) for output fields.
