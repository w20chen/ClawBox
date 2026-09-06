# ClawBox

ClawBox is a research system for running many LLM coding agents on Kunpeng
servers while controlling physical-memory use. It uses
[CubeSandbox](https://github.com/TencentCloud/CubeSandbox) for every virtual
machine and reuses [ClawTune](https://github.com/w20chen/ClawTune) for command
normalization, resource measurements, and memory predictions.

## How it works

Each agent owns two CubeSandbox virtual machines:

```text
Model API or recorded model responses
                 |
        Runtime VM (OpenClaw)
                 |
        memory check, then SSH
                 |
        Tool VM (workspace and tools)
```

The Runtime VM runs OpenClaw. The Tool VM owns the writable workspace and runs
commands through OpenSSH. Before a command starts, ClawBox checks the current
host memory and the command's predicted memory demand. During a long model
request, ClawBox can checkpoint both VMs and restore them when they are needed
again. Command measurements from cgroup v2 and eBPF are passed back to
ClawTune.

## Which document should I read?

| Goal | Read this |
| --- | --- |
| Run an experiment on an installed machine | [Experiment guide](docs/experiment-operations.md) |
| Understand or choose a comparison baseline | [Baseline guide](docs/baselines.md) |
| Find and interpret output files | [Results guide](docs/results-guide.md) |
| Install or repair CubeSandbox | [CubeSandbox setup](docs/cubesandbox-setup.md) |
| Understand research invariants | [Research design](docs/research-system-contract.md) |
| Find a maintenance script | [Script index](scripts/README.md) |

The historical Kunpeng Kubernetes notes are not required for normal use. They
remain in `docs/kunpeng920-reproduction-runbook.md` only as a record of the
older deployment.

## Quick start on an installed machine

Run commands from the ClawBox checkout. ClawBox and ClawTune should be sibling
directories.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'

mkdir -p "$HOME/.config/clawbox"
cp examples/clawbox-machine.env.example "$HOME/.config/clawbox/machine.env"
${EDITOR:-vi} "$HOME/.config/clawbox/machine.env"
set -a
. "$HOME/.config/clawbox/machine.env"
set +a

curl -fsS "$CUBE_API_URL/health"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
```

List the available baselines, then create a local experiment file:

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
```

Check the generated resource totals before running it:

```bash
.venv/bin/python -m clawbox.cli experiment describe \
  /data/clawbox-specs/my-experiment.yaml
.venv/bin/python -m clawbox.cli experiment validate \
  /data/clawbox-specs/my-experiment.yaml

RUN_ID="my-experiment-$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run /data/clawbox-specs/my-experiment.yaml --run-id "$RUN_ID"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment status "$RUN_ID"
```

`configure` writes a normal version-2 experiment YAML and does not start any
VMs. It refuses to overwrite a file unless `--force` is given. `describe`
shows the number of VMs, configured memory, memory-pool size, overcommit ratio,
selected policies, and number of experiment variants.

Before a formal high-concurrency run, follow the c1/c4/c8 connectivity and
identity checks in the [CubeSandbox setup guide](docs/cubesandbox-setup.md).
Resource totals and the c60 overcommit calculation are in the
[baseline guide](docs/baselines.md#resource-example) and are also printed by
`experiment describe`.

## Development check

```bash
python -m pytest -q
```

The supported runtime is CubeSandbox. Files for older Kubernetes, Kata, or
direct-Firecracker experiments are retained only for historical tests and are
not an alternative runtime for new results.
