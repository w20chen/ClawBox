"""Create editable inputs and a small binding to the existing experiment CLI."""
from __future__ import annotations

import json
from pathlib import Path
import shlex

import yaml

from .configuration import resolved_documents, compile_configuration, BaselineConfig, WorkloadConfig


def prepare(directory: Path, *, baseline: Path, workload: Path, machine_env: Path,
            output_root: Path, host_config: Path | None = None,
            creation_standard: Path | None = None) -> dict:
    directory = directory.resolve()
    if directory.exists():
        raise ValueError(f'preparation directory already exists: {directory}; reuse it or choose a new directory')
    selected = dict(machine_env=machine_env, host_config=host_config, creation_standard=creation_standard)
    for name, path in selected.items():
        if path is not None and not path.is_file():
            raise ValueError(f'{name} is not a file: {path}')
    if host_config:
        from .host import load_config
        load_config(host_config)
    if creation_standard:
        from .preflight import load_standard
        load_standard(creation_standard)
    b, w = resolved_documents(baseline, workload)
    spec = compile_configuration(BaselineConfig.model_validate(b), WorkloadConfig.model_validate(w))
    repository = Path(__file__).resolve().parents[2]
    bindings = dict(checkout=str(repository), output_root=str(output_root.resolve()),
                    **{k: str(v.resolve()) if v else None for k, v in selected.items()})
    constraints = []
    for key in ('host_config', 'creation_standard'):
        if bindings[key]:
            constraints.extend(['--' + key.replace('_', '-'), bindings[key]])
    script = '''#!/usr/bin/env bash
set -euo pipefail
DIRECTORY="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WRAPPER=(--machine-env MACHINE_ENV)
if [[ "${1:-}" == --sudo ]]; then WRAPPER+=(--sudo); shift; fi
if (( $# == 0 )); then
  echo 'Usage: bash experiment.sh [--sudo] preflight|launch|status|resume|abort|host ...' >&2
  exit 2
fi
COMMAND=$1; shift
INPUTS=()
case "$COMMAND" in
  preflight|launch|doctor|qualify|validate|plan|describe|resolve|run)
    INPUTS=(--baseline "$DIRECTORY/baseline.yaml" --workload "$DIRECTORY/workload.yaml" CONSTRAINTS) ;;
  resume) INPUTS=(CONSTRAINTS) ;;
esac
exec bash CLI_WRAPPER "${WRAPPER[@]}" --output-root OUTPUT_ROOT experiment "$COMMAND" "${INPUTS[@]}" "$@"
'''.replace('MACHINE_ENV', shlex.quote(bindings['machine_env'])).replace(
        'CLI_WRAPPER', shlex.quote(str(repository / 'scripts/clawbox'))).replace(
        'OUTPUT_ROOT', shlex.quote(bindings['output_root'])).replace('CONSTRAINTS', shlex.join(constraints))
    directory.mkdir(parents=True, exist_ok=False)
    for name, document in (('baseline', b), ('workload', w)):
        (directory / f'{name}.yaml').write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    (directory / 'experiment.sh').write_text(script, encoding='utf-8', newline='\n')
    (directory / 'bindings.json').write_text(json.dumps(bindings, indent=2) + '\n', encoding='utf-8')
    (directory / 'README.md').write_text('''# Run this experiment

Review `baseline.yaml` (system/policy) and `workload.yaml` (tasks/arrivals).
Input paths are absolute. Templates, timing, arrivals and policy are preserved.
`bindings.json` records the paths bound in `experiment.sh`; machine.env is referenced,
not copied. Keep those files and the selected checkout available. To change bindings,
prepare a new directory. Use a new run ID after editing experiment inputs.

From this directory on the configured Linux host:

```bash
bash experiment.sh --sudo preflight
bash experiment.sh --sudo launch --run-id YOUR_NEW_RUN_ID --detach
bash experiment.sh status YOUR_NEW_RUN_ID
# After interruption, with the original inputs:
bash experiment.sh --sudo resume YOUR_NEW_RUN_ID --detach
# To stop and clean only the selected run:
bash experiment.sh --sudo abort YOUR_NEW_RUN_ID
```

Preflight is read-only and exits nonzero with remedies when not ready. It does not
prove performance or create VMs. Launch rechecks readiness, reuses matching
qualification, and otherwise runs the existing real-VM qualification before
starting the experiment. Qualification completes before `--detach` returns.
After reboot, preview `host recover HOST.yaml --profile HOST.json`, then repeat
with `--apply` on an idle host and rerun preflight. See docs/guide.md and
docs/installation.md in the selected checkout for initial installation.
''', encoding='utf-8')
    return dict(directory=str(directory), experiment_id=spec.experiment_id, bindings=bindings,
                next=f'cd {shlex.quote(str(directory))} && bash experiment.sh --sudo preflight',
                scope='editable input copies and CLI binding; no host changes or qualification')
