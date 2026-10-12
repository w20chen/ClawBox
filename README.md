# ClawBox

ClawBox compares runtime memory congestion control, predictive admission, and
idle VM checkpointing for coding agents on standalone CubeSandbox. Each session
has an agent VM and a tool VM. ClawTune supplies per-command resource predictions
and measurements. Experiments support ordinary NUMA deployment and a supernode
configuration with a shared NUMA memory pool.

Start with the [中文自助操作指南](docs/self-service.md), covering new/existing hosts,
disk cleanup, reboot recovery, smoke runs, training, and policy comparisons.
From the checkout, `bash scripts/clawbox` automatically loads your saved machine
settings and Python environment; add `--sudo` for privileged experiment commands.
The supported command
interface is `clawbox experiment`; running it does not require a coding
assistant or Kubernetes. Live experiments require an ARM64 Linux/KVM host,
patched CubeSandbox, and compatible guest images and kernel files. The
repository does not include ready-to-run guest images.

Experiment YAML uses schema version 3. Runtime control, admission, and snapshot
reclamation are independent settings. The runtime controller uses the unmodified,
pinned AgentCgroup **cgroup v2 fallback**, adapted to stable VM priority groups;
it does not enable upstream BPF or CPU scheduling. Compare `runtime-resident`
with `predicted-resident` to measure the contribution of predictive admission,
or use the [four-policy ablation](examples/experiments/control-ablation.yaml).
Apply the same policy matrix separately to each host architecture.

| Task | Documentation |
| --- | --- |
| Configure a host, run the first experiment, then train and compare policies | [Step-by-step guide](docs/self-service.md) |
| Install services, import images, register VM templates, or update ClawTune | [Installation](docs/installation.md) |
| Change NUMA nodes, CPU lists, memory limits, the shared pool, or disk paths | [Host and memory configuration](docs/supernode.md) |
| Look up experiment fields, trace requirements, commands, recovery, and results | [Command and configuration reference](docs/guide.md) |
| Understand congestion control, admission, snapshots, memory accounting, and PMU limits | [Execution and measurement](docs/design.md) |
| Analyze a separate four-call CPU placement study | [Placement analysis](docs/placement.md) |

The [starter configuration](examples/experiments/getting-started.yaml) and
[smoke trace](examples/traces/smoke.jsonl) are the smallest example inputs.
The [example index](examples/README.md) distinguishes starter inputs, comparison
templates, and historical fixtures.
The training walkthrough uses a longer
[memory trace](examples/traces/memory-smoke.jsonl). Other example experiment
files contain host-specific paths or placeholder templates; configure them for
the target host before use. The files under `examples/predictions` are historical
fixtures, not current prediction artifacts. Generate a new frozen prediction
file with `clawbox experiment train` from separate, validated training runs.

Use `clawbox experiment --help` and each subcommand's `--help` for current
options. Experiment fields are defined in [spec.py](clawbox/experiments/spec.py).
Developers can run `python -m pytest`; update the relevant reference page
when changing a public option, and keep the full command sequence in the
step-by-step guide.

Keep generated results, local research data, host audit files, source checkouts,
and credentials outside version control; `.gitignore` covers the usual local
directories. Checked-in examples and vendored source (including its license and
provenance) remain part of the repository.

The [deprecated-components note](docs/deprecated.md) covers the old control
plane and `scripts/lab`. [Historical results](docs/archive/README.md) record
earlier configurations and measurements, not the current host state.
