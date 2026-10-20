# ClawBox

ClawBox runs controlled experiments on virtual machines used by coding agents.
It starts standalone CubeSandbox sessions, uses ClawTune predictions and
measurements, and compares when work starts, where it runs, and when VM state is
saved or restored.

Each session contains an agent VM and a tool VM. The supported interface is
`clawbox experiment`; the legacy service-based control plane is not supported.

## Start here

- [Install a host](docs/installation.md): install CubeSandbox, import images,
  register templates, and verify the VM path.
- [Run experiments](docs/guide.md): configure a host, create an experiment,
  validate it, run or resume it, and read the results.
- [Understand measurements](docs/design.md): VM execution, memory accounting,
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
bash scripts/clawbox experiment configure \
  examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/smoke.yaml" \
  --profile "$HOME/.config/clawbox/host.json" --recipe custom --concurrency 1
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/smoke.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/smoke.yaml" \
  --run-id smoke-01
```

Use `experiment schema` for the complete public field catalog and each
subcommand's `--help` for current options. Generated results, host audits,
credentials, prompts, and local research files are intentionally ignored by
Git. Checked-in examples and canonical traces are repository assets.
