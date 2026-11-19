# Experiment inputs

Start with [baselines/neutral.yaml](baselines/neutral.yaml) and
[workloads/smoke.yaml](workloads/smoke.yaml). Edit verified host/template identities
in the baseline before launching. Relative input paths are relative to the YAML.

```bash
bash scripts/clawbox experiment describe --baseline examples/baselines/neutral.yaml --workload examples/workloads/smoke.yaml
bash scripts/clawbox experiment validate --baseline examples/baselines/neutral.yaml --workload examples/workloads/smoke.yaml --inputs
```

`baselines/` contains system and method choices; `workloads/` contains tasks and
submission choices. Other workload JSON files are task manifests referenced by
`tasks.input`; traces and prediction files remain supporting assets. Host/cache
YAML files are preparation plans, not alternative experiment inputs.
See the [guide](../docs/guide.md) for host preparation, launch and cleanup and the
[reference](../docs/reference.md) for fields, defaults and constraints.
