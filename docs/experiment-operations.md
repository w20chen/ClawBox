# Experiment guide

This guide covers the normal CubeSandbox experiment workflow. If CubeSandbox
is not installed or its network is not working, start with the
[CubeSandbox setup guide](cubesandbox-setup.md).

## 1. Plan machine storage

ClawBox uses three different storage areas:

| Storage | Contains | Where it is configured |
| --- | --- | --- |
| CubeSandbox storage | VM layers, images, and checkpoints | CubeSandbox installation |
| VM writable disk | Runtime files and Tool `/workspace` | Immutable template registration |
| Experiment output | JSON, logs, telemetry, and summaries | CLI `--output-root` |

The template recipe below gives each Runtime VM a 20 GiB writable disk and each
Tool VM a 40 GiB writable disk. If those templates are used at c60, the nominal
offer is 3.6 TiB. Disk size is not recorded in the experiment YAML, so verify
it from the selected CubeSandbox templates. Sparse files or copy-on-write may
reduce actual storage, but the experiment must still leave enough space for
concurrent checkpoints and output.

Use ARM64, Python 3.12 or newer, cgroup v2, KVM, and a guest kernel with the
required eBPF/kprobe support.

## 2. Install ClawBox and build templates

Keep ClawBox and ClawTune as sibling checkouts and record both commits:

```bash
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone <ClawBox-repository> ClawBox
git clone <ClawTune-repository> ClawTune
cd ClawBox
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,postgres]'
```

Prepare the pinned CubeSandbox source additions and install its matching Python
SDK as described in [CubeSandbox setup](cubesandbox-setup.md):

```bash
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

Build the Runtime and Tool images on ARM64. Runtime contains OpenClaw and
ClawTune. Tool contains the workspace, SSH server, cgroup collector, and eBPF
collector.

```bash
export CLAWTUNE_ROOT="$HOME/src/ClawTune"
export REGISTRY='<registry>/clawbox'
export TAG="$(git rev-parse --short HEAD)"
export KERNEL_SHA='<guest-kernel-sha256-without-prefix>'

docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.runtime-cube -t "$REGISTRY/runtime-cube-arm64:$TAG" .
docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.tool-cube -t "$REGISTRY/tool-cube-arm64:$TAG" .
docker push "$REGISTRY/runtime-cube-arm64:$TAG"
docker push "$REGISTRY/tool-cube-arm64:$TAG"
```

Register new immutable templates from the pushed image digests:

```bash
export GUEST_KERNEL_COMPONENT='<CubeSandbox-kernel-component-id>'

.venv/bin/python scripts/register-cube-template.py \
  '<runtime-image>@sha256:<digest>' \
  --alias "clawbox-runtime-$TAG" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 2048 \
  --writable-layer-size 20G \
  --exposed-port 49983 --probe-port 49983 \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"

.venv/bin/python scripts/register-cube-template.py \
  '<tool-image>@sha256:<digest>' \
  --alias "clawbox-tool-$TAG" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 4096 \
  --writable-layer-size 40G \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"
```

Save the returned template IDs and image digests. The CPU and memory values in
an experiment YAML must match the templates. Changing the image, guest kernel,
or writable-disk size requires a new template.

## 3. Configure an installed machine

Create one machine-specific environment file outside Git:

```bash
mkdir -p "$HOME/.config/clawbox"
cp examples/clawbox-machine.env.example "$HOME/.config/clawbox/machine.env"
${EDITOR:-vi} "$HOME/.config/clawbox/machine.env"
set -a
. "$HOME/.config/clawbox/machine.env"
set +a
```

It contains CubeSandbox addresses, template IDs, result paths, and the
ClawTune checkout path. Model credentials remain separate.

Run the read-only checks after loading that file:

```bash
test -c /dev/kvm
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
curl -fsS "$CUBE_API_URL/health"
df -hT /data/cubelet "$CLAWBOX_OUTPUT_ROOT"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
git rev-parse HEAD
git -C "$CLAWTUNE_ROOT" rev-parse HEAD
```

After installation or reboot, run the c1/c4/c8 connectivity and identity gate
from the [CubeSandbox setup guide](cubesandbox-setup.md) before starting c60.

## 4. Create an experiment configuration

Generate a local YAML from a checked-in example:

```bash
.venv/bin/python -m clawbox.cli experiment configure \
  examples/experiments/openclaw-cube-replay-c60-overcommit.yaml \
  /data/clawbox-specs/my-run.yaml \
  --experiment-id my-run \
  --concurrency 1,5,60 \
  --runtime-memory-gib 2 --tool-memory-gib 4 \
  --pool-memory-gib 64 --checkpoint-headroom-gib 2 \
  --arrival-schedule fixed_stagger --stagger-seconds 0.2 \
  --baseline tool-static-resident \
  --baseline tool-static-eager-reactive
```

Useful option groups are:

| What to change | Options |
| --- | --- |
| Workload | `--trace`, `--case-id`, `--prompt`, `--validation-command`, `--repetitions` |
| Scale | `--concurrency`, arrival schedule, random seed, and timeouts |
| VM size | Runtime/Tool vCPU and memory in GiB |
| Memory control | pool size, host free-memory limit, restore headroom, static/full Tool memory |
| Policy | repeat `--baseline`; use `experiment baselines` to list names |
| Prediction | `--p90-kb`, model-wait estimate, fixed delay, and prefetch lead |
| Real model | `--inference-backend api`, `--base-url`, `--model`, `--api-key-env` |

Use `clawbox experiment configure --help` for the exact option names.
`configure` validates the schema and refuses to overwrite an existing file
unless `--force` is supplied. It also rejects a CPU or memory change unless a
new matching template is supplied. When changing a template, pass its template
ID, image reference, and image digest together; this prevents the YAML from
claiming a VM size or image that CubeSandbox will not actually use.

Inspect the final totals before running:

```bash
clawbox experiment describe /data/clawbox-specs/my-run.yaml
clawbox experiment validate /data/clawbox-specs/my-run.yaml
```

The offered-memory ratio shown by `describe` is:

```text
agents * (Runtime memory + Tool memory) / policy memory pool
```

For the c60 example it is `60 * (2 + 4) / 64 = 5.625x`. This compares
configured VM memory with the ClawBox policy pool. Actual host memory is a
separate measured result.

## 5. Run and monitor an experiment

Review the [baseline guide](baselines.md), then validate, plan, and run the
generated file:

```bash
export SPEC=/data/clawbox-specs/my-run.yaml
export RUN_ID="my-run-$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"

clawbox experiment baselines
clawbox experiment describe "$SPEC"
clawbox experiment validate "$SPEC"
clawbox experiment plan "$SPEC" > "$RUN_ID.plan.json"
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run "$SPEC" --run-id "$RUN_ID"
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment status "$RUN_ID"
```

`run` is synchronous and returns a nonzero status if any experiment variant
fails. Keep the failed output for diagnosis. Pair creation is limited by
`CLAWBOX_SANDBOX_CREATE_CONCURRENCY` (validated default: 8); this limits only
the startup burst, not the number of agents allowed to execute.

Monitor the host without changing it:

```bash
watch -n 2 'free -h; df -h /data/cubelet /data/clawbox-results'
```

## 6. Real model calls and replay

Large comparisons normally replay recorded model responses while keeping the
Runtime VM, OpenClaw, Tool VM, SSH commands, workspace, memory control, and
telemetry real. Use `time_scale: 1.0` for the primary snapshot comparison.

For a small real-model run, start from `examples/experiments/openclaw-cube.yaml`
and configure an OpenAI-compatible endpoint:

```bash
clawbox experiment configure \
  examples/experiments/openclaw-cube.yaml \
  /data/clawbox-specs/real-model-c1.yaml \
  --concurrency 1 \
  --inference-backend api \
  --base-url https://api.deepseek.com/v1 \
  --model <model-name> \
  --api-key-env OPENCLAW_API_KEY

export OPENCLAW_API_KEY='<provider-key>'
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run /data/clawbox-specs/real-model-c1.yaml --run-id real-model-c1
unset OPENCLAW_API_KEY
```

The credential stays in the Worker environment. Successful API runs write
recorded model traces under `<output-root>/<run-id>/model-traces/`. Reuse those
files in later replay configurations. Replay stops on a request or response
mismatch instead of silently choosing a different answer.

## 7. Build a frozen ClawTune P90 file

Use recording runs that are separate from the comparison workload:

```bash
mkdir -p /data/clawbox-kb
.venv/bin/python scripts/train-p90-from-runs.py \
  "$CLAWBOX_OUTPUT_ROOT/training-recording" \
  --clawtune-root "$CLAWTUNE_ROOT" \
  --repository '<repository-key>' \
  --training-set-id '<training-set-id>' \
  --idle-tool-vm-rss-mib '<measured-idle-rss>' \
  --host-calibration-observations \
    "$CLAWBOX_OUTPUT_ROOT/host-calibration/arms/<arm-id>.json" \
  --output /data/clawbox-kb/frozen-p90.json

sha256sum /data/clawbox-kb/frozen-p90.json
chmod a-w /data/clawbox-kb/frozen-p90.json
```

Pass this file with `--p90-kb`. Use the identical file for all compared P90
baselines and report its hash, prediction error, and fallback rate.

## 8. Finish and read the results

After every run:

```bash
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment collect "$RUN_ID" \
  > "$CLAWBOX_OUTPUT_ROOT/$RUN_ID.collect.json"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
sha256sum "$CLAWBOX_OUTPUT_ROOT/$RUN_ID/summary.json"
```

Clean only sandboxes attributed to this run. Do not remove templates, other
users' sandboxes, CubeSandbox storage, or earlier result directories as generic
cleanup. Use the [results guide](results-guide.md) to check correctness and
interpret performance, memory, prediction, and checkpoint measurements.
