# ClawBox

ClawBox runs controlled experiments on virtual machines used by coding agents.
It starts standalone CubeSandbox sessions, uses ClawTune predictions and
measurements, and compares when work starts, where it runs, and when VM state is
saved or restored.

Each session contains a Runtime VM running OpenClaw and a Tool VM executing task
commands. The supported interface is `clawbox experiment`; the legacy
service-based control plane is not supported.

## Start here

- [Install a host](docs/installation.md): install CubeSandbox, import images,
  register templates, and verify the VM path.
- [Run experiments](docs/guide.md): configure comparisons, train predictions,
  validate, launch, resume, and clean up.
- [Prepare workloads](docs/workloads.md): import recordings, build task images,
  refresh collectors, prove template sharing, and cache downloads.
- [Command and configuration reference](docs/reference.md): commands, fields,
  units, defaults, and requirements.
- [Read results](docs/results.md): artifacts, completion endpoints, logging,
  and overhead attribution.
- [Execution model](docs/design.md): VM execution, prediction, memory accounting,
  CPU and NUMA placement, snapshots, and hardware counters.
- [Deprecated components](docs/deprecated.md): code retained for reference but
  excluded from the supported workflow.

The smallest inputs are
[`six-dimension.yaml`](examples/experiments/six-dimension.yaml) and
[`smoke.jsonl`](examples/traces/smoke.jsonl). See the
[example index](examples/README.md) before using host-specific fixtures.

From a configured checkout:

```bash
bash scripts/clawbox experiment --help
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
```

Continue with the [smoke experiment](docs/guide.md#2-run-a-smoke-experiment).

Use `experiment schema` for the complete public field catalog and each
subcommand's `--help` for current options. Generated results, host audits,
credentials, prompts, and local research files are intentionally ignored by
Git. Checked-in examples and canonical traces are repository assets.
