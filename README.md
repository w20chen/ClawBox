# ClawBox

ClawBox compares memory admission, idle VM checkpointing, and restoration
policies for coding agents on standalone CubeSandbox. Each session has an agent
VM and a tool VM. ClawTune supplies per-command resource predictions and
measurements. The default configuration uses NUMA nodes 0 and 1 as two compute
nodes and NUMA node 2 as a shared memory pool.

Start with the [step-by-step guide](docs/self-service.md). The supported command
interface is `clawbox experiment`; running it does not require a coding
assistant or Kubernetes. Live experiments require an ARM64 Linux/KVM host,
patched CubeSandbox, and compatible guest images and kernel files. The
repository does not include ready-to-run guest images.

| Task | Documentation |
| --- | --- |
| Configure a host, run the first experiment, then train and compare policies | [Step-by-step guide](docs/self-service.md) |
| Install services, import images, register VM templates, or update ClawTune | [Installation](docs/installation.md) |
| Change NUMA nodes, CPU lists, memory limits, the shared pool, or disk paths | [Host and memory configuration](docs/supernode.md) |
| Look up experiment fields, trace requirements, commands, recovery, and results | [Command and configuration reference](docs/guide.md) |
| Understand admission, snapshots, memory accounting, and PMU limits | [Execution and measurement](docs/design.md) |
| Analyze a separate four-call CPU placement study | [Placement analysis](docs/placement.md) |

The [starter configuration](examples/experiments/getting-started.yaml) and
[smoke trace](examples/traces/smoke.jsonl) are the smallest example inputs.
The training walkthrough uses a longer
[memory trace](examples/traces/memory-smoke.jsonl). Other example experiment
and prediction files may contain host-specific paths, placeholder templates,
or older policies; review them before use on a new machine.

Use `clawbox experiment --help` and each subcommand's `--help` for current
options. Experiment fields are defined in [spec.py](clawbox/experiments/spec.py).
Developers can run `python -m pytest`; update the relevant reference page
when changing a public option, and keep the full command sequence in the
step-by-step guide.

The [deprecated-components note](docs/deprecated.md) covers the old control
plane and `scripts/lab`. [Historical results](docs/archive/README.md) record
earlier configurations and measurements, not the current host state.
