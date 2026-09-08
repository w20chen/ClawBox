# Administrative and development helpers

Experiment configuration, trace inspection, execution, status and reporting use
`clawbox experiment`; see the [user guide](../docs/guide.md). `scripts/clawbox`
only forwards arguments to that CLI. The former fixed-workload study launcher
has been removed.

The [installation guide](../docs/installation.md) provides complete commands for
asset export, template registration, endpoint checks and optional NUMA setup.
Other scripts are diagnostics, image/data preparation tools, or readers of
historical result formats. They are not alternate experiment launch interfaces.
