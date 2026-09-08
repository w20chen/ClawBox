# Supported commands and scripts

Start with `bash scripts/clawbox study --help`.

| Command | Purpose |
| --- | --- |
| `study init` | Create a machine-specific YAML |
| `study setup-memory` | Configure memory on an idle host |
| `study check` | Check files and memory prerequisites |
| `study start` | Start the baseline sequence in the background |
| `study status` | Show completed results and live progress |
| `study report` | Print the comparison table |

The wrapper loads `~/.config/clawbox/machine.env`. See the [experiment guide](../docs/experiment-operations.md).

Provisioning helpers include `export-machine-assets.sh`, `register-cube-template.py`, `setup-tiered-memory.sh`, and `deploy/cubesandbox/prepare-semantic-source.sh`.

The endpoint, replay-output audit, tier-storage, guest-artifact, and kprobe validation scripts remain supported. Lower-level probes diagnose the same CubeSandbox backend; they are not alternative deployment paths.

Image entrypoints, benchmark image builders, and prediction/trace preparation tools remain where the supported research workflow needs them. Historical Kubernetes, Kata, and direct-Firecracker installation scripts have been removed.
