# ClawBox

ClawBox runs coding agents in standalone CubeSandbox virtual machines and compares 13 memory-management policies. Each agent has an OpenClaw Runtime VM and a separate Tool VM. ClawTune supplies tool predictions and clause-level resource measurements.

Kubernetes, Kata, and direct-Firecracker deployment are not supported.

## Start here

- [Use an installed machine](docs/experiment-operations.md)
- [Install on a new machine](docs/cubesandbox-setup.md)
- [Current kunpeng environment](docs/current-environment.md)
- [Baselines](docs/baselines.md) and [results](docs/results-guide.md)
- [Research design](docs/research-system-contract.md) and [memory tiers](docs/tiered-memory-simulation.md)

## Everyday commands

Run these from an installed ClawBox checkout on Linux:

```bash
bash scripts/clawbox study --help
bash scripts/clawbox study check --spec /data/clawbox-specs/rec-a.yaml
bash scripts/clawbox study start --spec /data/clawbox-specs/rec-a.yaml --name check-01 --steps 5
bash scripts/clawbox study status /data/clawbox-results/check-01
bash scripts/clawbox study report /data/clawbox-results/check-01
```

The wrapper loads `~/.config/clawbox/machine.env`. Set `CLAWBOX_MACHINE_ENV` to use another file. A study continues after SSH disconnects. Baselines run sequentially, with 40 agent sessions offered per baseline.

Five recorded rounds are a short correctness check, not a complete coding task. Use `--full-trace` for the full recording. A running process or a timed-out run is not a successful experiment.

## Development

Keep ClawBox and ClawTune in sibling directories. With Python 3.12 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests
```

Real experiments also need the patched CubeSandbox server and matching SDK. Unit tests do not replace VM, replay, and eBPF checks. Keep generated logs, results, virtual environments, and temporary checkouts outside Git.
