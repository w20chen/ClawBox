# ClawBox

ClawBox compares memory reservation, idle-VM reclamation, and restoration policies
for coding-agent workloads on standalone CubeSandbox. Each session has a Runtime
VM and a separate Tool VM. ClawTune supplies command predictions and measurements.

The public interface is `clawbox experiment`. Workloads, concurrency, resources,
and policy dimensions are configuration inputs; no particular benchmark is required.

## Start

With Python 3.12 or newer, from this checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
clawbox experiment trace examples/traces/smoke.jsonl
clawbox experiment validate examples/experiments/getting-started.yaml --inputs
clawbox experiment describe examples/experiments/getting-started.yaml
```

These commands need no VM host. Running the example requires registered ARM64
CubeSandbox templates; the included template aliases are placeholders.

| Document | Contents |
| --- | --- |
| [User guide](docs/guide.md) | Configuration, policy dimensions, trace format, recording, execution, results |
| [Installation](docs/installation.md) | Host dependencies, guest artifacts, server/SDK setup, templates, memory checks |
| [Design contracts](docs/design.md) | Admission ordering, memory accounting, checkpoint semantics, evidence requirements |

The included [experiment](examples/experiments/getting-started.yaml) and
[trace](examples/traces/smoke.jsonl) form a small direct-command replay check.
Real agent runs use the same CLI with `agent.driver: openclaw` and live or recorded
model responses. Other checked-in experiment files are research fixtures with
machine-specific references, not installation defaults.

## Development

ClawTune is installed from `main`; a sibling checkout or `CLAWTUNE_SIDECAR_SRC`
can supply its source. Use the [update instructions](docs/installation.md#update-clawtune-from-main)
to build Runtime and Tool images from the same latest-main export.
Run relevant checks with `python -m pytest`.
Guest integration requires the patched server, matching SDK, images and kernel
listed in the installation guide. Store generated outputs outside Git.
