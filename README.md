# ClawBox

ClawBox is a research system for high-density CPU-side LLM Agent execution on
Kunpeng. It uses [CubeSandbox](https://github.com/TencentCloud/CubeSandbox) as its only VM/sandbox substrate and adds
Agent-aware memory admission, [ClawTune](https://github.com/w20chen/ClawTune) resource prediction, and VM
snapshot/reclamation above it.

## System design

Each logical Agent owns exactly two CubeSandbox VMs - Runtime VM and Tool VM.

```text
Model provider or deterministic replay
                 |
          host ModelGateway
                 |
        Runtime VM: OpenClaw + ClawTune
                 |
       synchronous admission metadata
                 +------> host PolicyControl
                 |
              native SSH
                 |
        Tool VM: /workspace + real tools
                 +------> cgroup-v2 + eBPF telemetry
```

Runtime VM performs [OpenClaw](https://github.com/openclaw/openclaw) execution. Tool VM owns the mutable workspace
and executes the OpenClaw tools `exec`, `process`, `read`, `write`, `edit`, and `apply_patch`
through native SSH. PolicyControl returns `ADMIT` or blocks before SSH begins;
it never proxies commands, output, or files.

Every Tool execution keeps one `(session_id, execution_id)` across prediction,
admission, SSH, cgroup/eBPF measurement, completion, and ClawTune feedback.
Guest Tool memory profiles commands; host VM memory measures density and
snapshot reclamation. These metrics are intentionally separate.

During a useful model wait, `snapshot_pause` can pause Tool VM and Runtime VM after
active SSH completes. Runtime VM is restored before ModelGateway releases the
pending response. Tool VM remains paused until the next Tool admission, when its
CubeSandbox TCP endpoint is resolved again and its epoch advances. `resident`
keeps both VMs in memory for the Agent lifetime.

The complete scientific invariants are in
[the research system contract](docs/research-system-contract.md).

## Start here

- [Experiment operations](docs/experiment-operations.md): new machine,
  existing machine, disk/volume sizing, templates, every baseline and
  hyperparameter, replay, real LLM, P90 training, c60, and cleanup.
- [Results guide](docs/results-guide.md): where run artifacts live, how to copy
  them from Kunpeng, validity gates, and how to interpret throughput, memory,
  admission, P90, and snapshot metrics.
- [Baseline definitions](docs/baselines.md): exact admission/residency
  semantics and the intended scientific question for every policy.
- [CubeSandbox setup](docs/cubesandbox-setup.md): semantic port 2222 API,
  standalone deployment, network/identity gates, and pause/restore behavior.
- [Kunpeng reproduction](docs/kunpeng920-reproduction-runbook.md): historical
  host storage, ARM64 guest-kernel, S3lvol, and template preparation details.
- [Script index](scripts/README.md): supported operator helpers.

## Quick start on an installed machine

ClawBox and ClawTune should be adjacent checkouts. Use the interpreter from the
ClawBox checkout so an older global installation cannot run accidentally:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'

umask 077
mkdir -p "$HOME/.config/clawbox"
cp examples/clawbox-machine.env.example "$HOME/.config/clawbox/machine.env"
${EDITOR:-vi} "$HOME/.config/clawbox/machine.env"
set -a
. "$HOME/.config/clawbox/machine.env"
set +a

curl -fsS "$CUBE_API_URL/health"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
```

Create a machine-local experiment from the validated c60 shape. This command
changes only configuration and does not start VMs:

```bash
.venv/bin/python -m clawbox.cli experiment baselines

.venv/bin/python -m clawbox.cli experiment configure \
  examples/experiments/openclaw-cube-replay-c60-overcommit.yaml \
  /data/clawbox-specs/my-experiment.yaml \
  --experiment-id my-experiment \
  --concurrency 1,5,60 \
  --runtime-memory-gib 2 --tool-memory-gib 4 \
  --pool-memory-gib 64 --checkpoint-headroom-gib 2 \
  --baseline tool-static-resident \
  --baseline tool-static-eager-reactive

.venv/bin/python -m clawbox.cli experiment describe \
  /data/clawbox-specs/my-experiment.yaml
.venv/bin/python -m clawbox.cli experiment validate \
  /data/clawbox-specs/my-experiment.yaml
.venv/bin/python -m clawbox.cli experiment plan \
  /data/clawbox-specs/my-experiment.yaml
RUN_ID="my-experiment-$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run /data/clawbox-specs/my-experiment.yaml --run-id "$RUN_ID"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment status "$RUN_ID"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment collect "$RUN_ID"
```

`configure` accepts friendly GiB values and canonical baseline names, writes a
normal schema-v2 YAML, and refuses to overwrite an existing file unless
`--force` is supplied. `describe` prints VM count, offered vCPU and memory,
policy-pool size, overcommit ratio, policy tuples, and total arm count before a
run. See [Experiment operations](docs/experiment-operations.md#5-yaml-parameters-and-memory-overcommit)
for all supported overrides and new-machine template provenance.

Before experiments, validate current immutable Runtime/Tool template IDs and
digests at c1, then c4/c8:

```bash
for count in 1 4 8; do
  .venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
    --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
    --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
    --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
    --count "$count" \
    --output "$CLAWBOX_OUTPUT_ROOT/endpoint-c${count}.json"
done
```

## Baselines

Schema-v2 policy tuples independently select admission and residency:

| Comparison | Admission | Reclamation / restore |
| --- | --- | --- |
| Lifetime conservative | `lifetime_full` | `resident` |
| Full Tool reservation | `tool_full` | `resident` |
| Static Tool estimate | `tool_static` | `resident` |
| Command-specific prediction | `tool_p90` | `resident` |
| Reclamation-only | `tool_static` | `snapshot_pause`, eager/reactive |
| Combined P90 snapshot | `tool_p90` | `snapshot_pause`, eager/reactive |
| Decision variants | `tool_p90` | fixed-delay or wait-aware, reactive/proactive |
| Oracle upper bound | `tool_oracle` | explicitly labeled evaluation-only |

The exact YAML for each baseline and its required resource fields is in
[Experiment operations](docs/experiment-operations.md#6-run-every-baseline).
Checked-in examples are under `examples/experiments/`. The current c60 smoke
shape is `openclaw-cube-replay-c60-overcommit.yaml`.

## Memory overcommit

`execution.concurrency_levels` is offered Agent workload. Admission is driven
by the configured physical-memory pool, predicted incremental Tool memory,
reservations, restore headroom, and a common host free-memory guard.

```text
offered ratio = concurrency * (Runtime MiB + Tool MiB) / pool budget MiB
```

The validated c60 configuration offers 360 GiB of VM memory against a 64 GiB
policy pool, or 5.625x scoped overcommit. This is deliberately a claim about
the experimental memory scope, not exhaustion of the entire shared host.
`CLAWBOX_SANDBOX_CREATE_CONCURRENCY` only bounds provisioning fan-out; it is
not an Agent execution semaphore.

## Replay, ClawTune, and real models

Deterministic managed replay is the primary large-scale method. Only model
generation is replayed. Runtime/OpenClaw, native SSH, Tool VM, workspace,
commands, PolicyControl, snapshot/restore, memory pressure, cgroup, and eBPF
remain real. Use recorded wait timing (`time_scale: 1.0`) for primary snapshot
results and fail closed on divergence.

Build the P90 KB from separate recording data with
`scripts/train-p90-from-runs.py`, freeze and hash it, and reuse the same file
across policy arms. The live path supports exact-command source/fallback,
predicted versus actual memory, reservation, blocked time, execution duration,
and telemetry validity.

Real OpenAI-compatible model calls use `inference.backend: api`. Provider
credentials are read only from the Worker environment named by
`api_key_env`; they do not enter the YAML or Runtime VM. Successful API runs
export exact replay traces under the run's `model-traces/` directory.

## Verified implementation state

The current managed path has passed on Kunpeng with real CubeSandbox
Runtime/Tool pairs:

- c60 resident and paired snapshot: 60/60 correct, exact telemetry joins,
  zero telemetry loss, zero host OOM, and zero sandbox leaks under 5.625x scoped
  memory overcommit;
- frozen ClawTune P90 resident and snapshot c5: all predictions from the
  immutable exact-command KB with zero fallback;
- real OpenClaw + DeepSeek c1 and c2;
- Runtime-first response release, lazy Tool restore, endpoint re-resolution,
  stable Tool identity, lifecycle timing, and physical-memory measurement.

These are implementation and scale gates. Formal paper claims still require
multiple representative heterogeneous held-out trajectories, a disjoint
multi-command frozen KB, fixed resource/NUMA scope, and repeated arms.

## Development verification

```bash
python -m pytest -q
python scripts/audit-experiment-matrices.py
docker run --rm -e GOPROXY=https://goproxy.cn,direct \
  -v "$PWD/toolbridge:/src" -w /src golang:1.25-bookworm go test ./...
```

Do not use legacy Kubernetes Pods/Jobs/SandboxTasks, direct Firecracker,
NodePort, Redis lookup, guest-IP fallback, or an SSH proxy as a second runtime
path. CubeSandbox remains the sole sandbox substrate.
