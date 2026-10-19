# ClawBox

ClawBox is a research system for controlled experiments on coding-agent VM
scheduling. It runs standalone CubeSandbox sessions, uses ClawTune predictions
and measurements, and compares admission, runtime control, CPU placement, idle
checkpointing, shared-memory borrowing, and checkpoint storage policies.

Each session contains an agent VM and a tool VM. The supported interface is
`clawbox experiment`; the legacy service-based control plane is not supported.

## Start here

- [Install a host](docs/installation.md): install CubeSandbox, import images,
  register templates, and verify the VM path.
- [Run experiments](docs/guide.md): configure a host, create a schema-4
  experiment, validate, qualify, launch, recover, and read results.
- [Understand the model](docs/design.md): execution, accounting, NUMA,
  checkpoint, PMU, and measurement semantics.
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
