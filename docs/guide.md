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
  --local-gib 36 --low-gib 28 --high-gib 32 \
  --warm --warm-gib 128 --shared-borrow-percent 50
```

Setup starts the installed CubeSandbox services, configures the LOCAL memory
cgroup, configures the optional tmpfs WARM pool, then creates, executes, and
destroys a probe VM. It saves `~/.config/clawbox/host.json` only after every
check succeeds. It does not stop unknown VMs or delete results, templates, or
images. With `--warm`, setup checks the installed CubeSandbox Python SDK and,
when needed, applies only the tiered/incremental SDK hunks from this repository
to `--cube-source` and installs that SDK. The successful source path is saved in
the host profile, so later setup runs do not need the flag.

This configuration advertises the VM capacity selected by the experiment (for
the c16, 6 GiB-per-agent study: 96 GiB) while controlling NUMA0 with
LOW/HIGH/HARD = 28/32/36 GiB. Crossing HIGH stops capacity growth and makes
A+B+C checkpoint safe waiting VMs until LOCAL reaches LOW. Crossing HIGH is
allowed and measured; it is not a failure. HARD triggers the live-VM safety
path: a whole VM cgroup is rebound to NUMA1 while it continues running. Live
borrow reservations are charged at configured VM capacity, may use at most 50%
of the 128 GiB shared pool, and share that pool with in-flight and committed
snapshots. The parent VM cgroup therefore has a 100 GiB combined live limit
(36 GiB LOCAL plus 64 GiB borrow); 36 GiB remains the NUMA0 tier boundary, not
the combined cgroup `memory.max`.

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
  --static-tool-memory-mib auto \
  --concurrency 1,4,8,16 \
  --pool-memory-gib 32 \
  --snapshot-storage warm-only \
  --model-wait-prediction-seconds auto
```

`auto` freezes the recorded trace's median model duration after replay time
scaling and records the trace digest as its source. Its value and source are
part of the experiment identity. Use at least three randomized repetitions for
a comparative result that reports run-to-run uncertainty; a one-repetition run
is only a functional pilot. All policies in one specification share the same
workload, concurrency, VM shapes, pool budget, arrival schedule, and validation.
`auto` takes A's fixed reservation from the validated training run's measured
extra-memory P90. `describe` prints physical LOCAL capacity, the smaller policy
budget when one is configured, and reservation-only admission ceilings; those
ceilings deliberately exclude resident VM memory, which is measured and enforced
during the run.

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
clawbox --output-root /data/clawbox-results experiment report eval-01
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
to telemetry, task validation passes, no OOM occurs, live borrow stays within its
64 GiB cap, combined NUMA1 live residency plus snapshot claims stays within the
128 GiB shared pool, and every owned VM is confirmed absent after cleanup.

The run root contains:

| Path | Contents |
| --- | --- |
| `run-state.json` | Authoritative live or terminal state |
| `experiment.yaml`, `qualification.json` | Frozen configuration and qualification receipt |
| `summary.json`, `summary.csv`, `summary.md` | Results for completed arms |
| `arms/*.json`, `arms/*.complete` | Canonical arm result and verified-success marker |
| `attempts/ATTEMPT/ARM/` | Logs, events, gateway state, telemetry, and ownership journal for one attempt |

For a valid memory-overcommit result, compare throughput, JCT, admission wait,
physical LOCAL memory, time and GiB-seconds above HIGH, live-borrow count/cost,
checkpoint/restore cost, and validation under the same offered concurrency.
Transient LOCAL use above 32 GiB is expected prediction error and remains valid.
An OOM, shared-pool overflow, incomplete telemetry, failed validation, or
unverified cleanup invalidates the arm as performance evidence.

`experiment report` reads completed arm records and event streams and reports
those fields together with WARM transfer/commitment and prediction coverage,
source, and error. It keeps configured VM capacity, reservations, predictions,
measured NUMA0 use, live NUMA1 residency, and snapshot bytes as separate
quantities.

See [host workflow](lab.md) for the same sequence in Chinese and
[installation](installation.md) for installing the patched CubeSandbox services.
