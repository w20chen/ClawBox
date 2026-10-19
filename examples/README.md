# Configuration and input examples

Start with the [experiment guide](../docs/guide.md). It uses
`clawbox experiment configure` to fill in the applied host's templates, NUMA
layout and capacities. Save generated experiment YAMLs outside this directory.

| File or directory | Use |
| --- | --- |
| [clawbox-machine.env.example](clawbox-machine.env.example) | Copy to `~/.config/clawbox/machine.env` and supply this host's addresses, source paths and image/template identities. |
| [experiments/six-dimension.yaml](experiments/six-dimension.yaml) | Schema-4 starter; use `configure --recipe` and review the six-dimension matrix. |
| [experiments/getting-started.yaml](experiments/getting-started.yaml) | Starting configuration for a small end-to-end experiment; configure its placeholders before launching. |
| [traces/smoke.jsonl](traces/smoke.jsonl) | Minimal replay input for checking the execution path. |
| [traces/memory-smoke.jsonl](traces/memory-smoke.jsonl) | Longer memory workload used by the training walkthrough. |
| [traces/llc-shared-chase-early.jsonl](traces/llc-shared-chase-early.jsonl) | Canonical shared-LLC replay input with the long call early in each session. |
| [traces/llc-shared-chase-late.jsonl](traces/llc-shared-chase-late.jsonl) | Canonical shared-LLC replay input with the long call late in each session. |
| [experiments/control-ablation.yaml](experiments/control-ablation.yaml) | Four-policy comparison; configure the host and provide a separate validated training artifact. |
| Other experiment YAMLs and traces | Workload or historical host examples. Check their comments, template identities, repository dependencies and paths before use. |
| [predictions/](predictions/) | Historical prediction fixtures. Generate current artifacts with `clawbox experiment train`. |
| [clawbox-snapshot-host.env.example](clawbox-snapshot-host.env.example) | Older single-node helper settings. New deployments use the editable host YAML described in the experiment guide. |

Do not put credentials, generated predictions, local traces or experiment results
into these checked-in examples. The command/configuration reference is in
[docs/guide.md](../docs/guide.md).
