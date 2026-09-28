# Configure, qualify, and run experiments

The supported interface is `clawbox experiment`. It runs OpenClaw workloads in
standalone CubeSandbox Runtime and Tool VMs. The host setup, qualification,
formal run, cleanup, and result state all use this one command group.

## 1. Inspect inputs without a VM host

```bash
clawbox experiment baselines
clawbox experiment validate examples/experiments/getting-started.yaml --inputs
clawbox experiment describe examples/experiments/getting-started.yaml
clawbox experiment plan examples/experiments/getting-started.yaml
```

The active comparison contains three complete policy tuples:

| Ablation | Baseline | Behavior |
| --- | --- | --- |
| A | `tool-static-resident` | Calibrated fixed Tool-memory admission; VMs remain resident |
| A+B | `tool-p50-resident` | Frozen command-specific P50 admission; VMs remain resident |
| A+B+C | `tool-p50-wait-reactive` | P50 admission plus wait-aware WARM checkpoint and reactive restore |

`clawbox experiment baselines --all` shows retained research policies with a
`DEPRECATED` label. Formal standalone runs reject those policies and reject a
hand-written policy whose name and tuple do not match one of the three entries
above.

## 2. Configure the host

Run setup on the ARM64 Linux/KVM host:

```bash
clawbox experiment setup \
  --runtime-template RUNTIME_TEMPLATE_ID \
  --tool-template TOOL_TEMPLATE_ID \
  --node NODE_ID \
  --cube-source "$HOME/src/CubeSandbox" \
  --local-gib 64 \
  --warm --warm-gib 128
```

Setup starts the installed CubeSandbox services, configures the LOCAL memory
cgroup, configures the optional tmpfs WARM pool, then creates, executes, and
destroys a probe VM. It saves `~/.config/clawbox/host.json` only after every
check succeeds. It does not stop unknown VMs or delete results, templates, or
images. With `--warm`, setup checks the installed CubeSandbox Python SDK and,
when needed, applies only the tiered/incremental SDK hunks from this repository
to `--cube-source` and installs that SDK. The successful source path is saved in
the host profile, so later setup runs do not need the flag.

After changing ClawTune, rebuild both guest integrations:

```bash
clawbox experiment images \
  --registry REGISTRY/clawbox \
  --go /path/to/go \
  --kernel-source /path/to/linux-source \
  --kernel-build /path/to/linux-build \
  --direct-network
```

This command builds the current ClawTune plugin and sidecar, embeds the pinned
ARM64 mvdan parser, pushes immutable images, registers new CubeSandbox templates,
and updates the host profile. Formal runs verify the exact ClawTune revision and
image digests. A successful first build saves these non-secret build inputs in
the host profile, so later ClawTune upgrades only require:

```bash
clawbox experiment images
```

Use `--proxy-network` once to replace a saved direct-network choice.

## 3. Create training and evaluation specifications

Use `configure` instead of editing resource identities by hand. A training run
uses A only and records complete CubeSandbox guest-memory measurements:

```bash
clawbox experiment configure examples/experiments/getting-started.yaml train.yaml \
  --experiment-id train-memory \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --baseline tool-static-resident \
  --concurrency 16 \
  --pool-memory-gib 32
```

After that run succeeds, freeze P50 predictions from its results:

```bash
clawbox experiment train /data/clawbox-results/train-01 \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --output /data/clawbox/predictions/eval-p50.json
```

The artifact stores both LatticeKB and ToolKB evidence for each command. ClawBox
selects LatticeKB first and falls back to ToolKB; this selection does not modify
ClawTune. Missing observations are never converted to zero.

Create the A/A+B/A+B+C evaluation from the same VM shapes and trace:

```bash
clawbox experiment configure examples/experiments/getting-started.yaml eval.yaml \
  --experiment-id memory-overcommit-eval \
  --trace /data/replay.jsonl \
  --repository owner/repo \
  --baseline tool-static-resident \
  --baseline tool-p50-resident \
  --baseline tool-p50-wait-reactive \
  --prediction-artifact /data/clawbox/predictions/eval-p50.json \
  --concurrency 1,4,8,16 \
  --pool-memory-gib 32 \
  --snapshot-storage warm-only \
  --model-wait-prediction-seconds 3 \
  --model-wait-prediction-source separate-training-run
```

Use a model-wait prediction measured in a separate run; its value and source are
part of the experiment identity. Keep one repetition unless the study explicitly
requires uncertainty estimates. All policies in one specification share the same
workload, concurrency, VM shapes, pool budget, arrival schedule, and validation.

## 4. Validate and qualify

```bash
clawbox experiment validate eval.yaml --inputs
clawbox experiment describe eval.yaml
clawbox experiment doctor eval.yaml --probe-vm
clawbox --output-root /data/clawbox-results experiment qualify eval.yaml
```

Qualification uses the first model step, which must contain a Tool call, at the
largest configured concurrency and across every selected policy. It creates real
VMs, exercises OpenClaw, prediction, telemetry, checkpoint/restore policy paths,
validation, hard process termination, and cleanup. A successful receipt is saved
beside the YAML and is bound to the exact spec digest, policies, concurrency,
node, and image digests. Any change requires a new qualification.

## 5. Run and recover

```bash
clawbox --output-root /data/clawbox-results \
  experiment run eval.yaml --run-id eval-01 --detach

clawbox --output-root /data/clawbox-results experiment status eval-01
clawbox --output-root /data/clawbox-results experiment resume eval-01 --detach
clawbox --output-root /data/clawbox-results experiment abort eval-01
clawbox --output-root /data/clawbox-results experiment destroy eval-01
```

Each arm runs in a separate worker process under a hard deadline. The supervisor
updates `run-state.json` atomically with its PID identity, current phase, arm,
heartbeat, event-file progress, and cleanup result. A recorded `running` state
with a dead supervisor is reported as `orphaned`. Resume first terminates and
cleans an interrupted worker, then skips only successful arms whose spec digest
and `cleanup_verified` marker match.

## 6. Interpret results

An arm succeeds only when every requested session completes, every model response
is delivered, at least one native Tool call executes, execution IDs join exactly
to telemetry, task validation passes, the LOCAL pool and host OOM counters remain
within limits, and every owned VM is confirmed absent after cleanup.

The run root contains:

| Path | Contents |
| --- | --- |
| `run-state.json` | Authoritative live or terminal state |
| `experiment.yaml`, `qualification.json` | Frozen configuration and qualification receipt |
| `summary.json`, `summary.csv`, `summary.md` | Results for completed arms |
| `arms/*.json`, `arms/*.complete` | Canonical arm result and verified-success marker |
| `attempts/ATTEMPT/ARM/` | Logs, events, gateway state, telemetry, and ownership journal for one attempt |

For a valid memory-overcommit result, compare throughput, JCT, admission wait,
physical LOCAL memory, checkpoint/restore cost, and validation under the same
offered concurrency. A run with `pool_budget_exceeded`, an OOM increment,
incomplete telemetry, failed validation, or unverified cleanup is a failed arm,
not performance evidence.

See [host workflow](lab.md) for the same sequence in Chinese and
[installation](installation.md) for installing the patched CubeSandbox services.
