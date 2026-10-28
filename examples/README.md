# Configuration and input examples

Start with the [experiment walkthrough](../docs/guide.md). It uses
`clawbox experiment configure` to fill in the applied host's templates, NUMA
layout, and capacities. Standard new profiles use 1 GiB Runtime and 3 GiB Tool VMs;
register matching templates and apply the host profile before configuring a run.
Frozen reproduction specs and historical host examples preserve their original
VM shapes and measurements. Save generated experiment YAMLs outside this directory.

| File or directory | Use |
| --- | --- |
| [clawbox-machine.env.example](clawbox-machine.env.example) | Copy to `~/.config/clawbox/machine.env` and supply this host's addresses, source paths and image/template identities. |
| [experiments/six-dimension.yaml](experiments/six-dimension.yaml) | Current starter file; use `configure --recipe` and review the generated comparison. |
| [experiments/getting-started.yaml](experiments/getting-started.yaml) | Starting configuration for a small end-to-end experiment; configure its placeholders before launching. |
| [traces/smoke.jsonl](traces/smoke.jsonl) | Minimal replay input for checking the execution path. |
| [traces/memory-smoke.jsonl](traces/memory-smoke.jsonl) | Longer memory workload used by the training walkthrough. |
| [traces/llc-shared-chase-early.jsonl](traces/llc-shared-chase-early.jsonl) | Canonical shared-LLC replay input with the long call early in each session. |
| [traces/llc-shared-chase-late.jsonl](traces/llc-shared-chase-late.jsonl) | Canonical shared-LLC replay input with the long call late in each session. |
| [experiments/llc-shared-c16-b32-20261019.yaml](experiments/llc-shared-c16-b32-20261019.yaml) and [reverse order](experiments/llc-shared-c16-b32-reverse-20261019.yaml) | Frozen specifications used by the one-command LLC reproduction. |
| [predictions/llc-shared-prediction-progress.json](predictions/llc-shared-prediction-progress.json) | Frozen prediction data for the shared-cache reproduction; its companion knowledge base is in the same directory. |
| [experiments/control-ablation.yaml](experiments/control-ablation.yaml) | Four-policy comparison; configure the host and provide a separate validated training artifact. |
| Other experiment YAMLs and traces | Workload or historical host examples. Check their comments, template identities, repository dependencies and paths before use. |
| [predictions/](predictions/) | Historical prediction fixtures. Generate current artifacts with `clawbox experiment train`. |
| [clawbox-snapshot-host.env.example](clawbox-snapshot-host.env.example) | Older single-node helper settings. New deployments use the editable host YAML described in the experiment guide. |

Do not put credentials, other generated predictions, local traces or experiment
results into these checked-in examples. The command/configuration reference is in
[docs/reference.md](../docs/reference.md). For imported recordings and task images,
see [workload preparation](../docs/workloads.md).
