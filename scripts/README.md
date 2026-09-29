# Administrative and development helpers

For first-time use, follow the [self-service workflow](../docs/self-service.md):
`clawbox experiment host inspect/init/check/apply` provides an editable host plan
and checks prerequisites before applying NUMA and storage settings.

`bash scripts/lab setup --warm`, `baselines`, `run TASK.yaml`, and `cleanup RUN_DIR`
provide the [daily host workflow](../docs/lab.md). They use the existing experiment
runner and default to RAM-only snapshots; COLD storage requires explicit selection.

Experiment configuration, trace inspection, execution, status and reporting use
`clawbox experiment`; see the [user guide](../docs/guide.md). `scripts/clawbox`
only forwards arguments to that CLI. The former fixed-workload study launcher
has been removed.

The [installation guide](../docs/installation.md) provides complete commands for
asset export, template registration, endpoint checks and optional NUMA setup.
For tiered snapshots, `snapshot-host.sh HOST_ENV check` prints a read-only JSON
diagnosis; `snapshot-host.sh HOST_ENV apply` configures an idle host from the same
editable file. Copy [the example](../examples/clawbox-snapshot-host.env.example).
The [kunpeng host example](../deploy/hosts/kunpeng.snapshot.env.example) records
its current non-secret paths and needs capacity review before use.
Other scripts are diagnostics, image/data preparation tools, or readers of
historical result formats. They are not alternate experiment launch interfaces.
