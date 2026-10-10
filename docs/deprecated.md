# Supported workflow and deprecated modules

Use `clawbox experiment` for new deployments and experiments. CubeSandbox
execution, ClawTune predictions and measurements, replay, and result
collection remain supported.

The former service-based control plane is deprecated: `clawbox.api`, `managed`,
`scheduler`, `controller`, `allocator`, `node_agent`, `tool_agent`, `ingester`, and
orchestration in `cell`. Their source is retained for reference, not as
supported deployment entry points. Future changes need not preserve these
modules' behavior, historical APIs, schemas, aliases, or stored-state formats.
Do not deploy these services for the current experiment workflow.

The parallel `scripts/lab` command-line interface is also deprecated. Its shared
host setup and image helpers are still called by `clawbox experiment setup` and
`clawbox experiment images`; use the public experiment commands for new runs.

The supported experiment uses schema version 3 and the current presets printed by
`clawbox experiment baselines`. Historical policy names, schemas and prediction
artifacts are not accepted. Deprecated verification scripts and control-plane
workflows retain their source but do not constrain current experiment tests.

This does not deprecate shared functionality still used by current commands:

- `cell.p90` still exports prediction data for current training commands.
- `replay` provides the active model gateway, trace reader, and execution types.
- `common` contains types and database support still used by native tuning.
- `benchmark` and placement helpers are auxiliary tools, not replacements for
  the experiment entry point.

These dependencies must be maintained for their current callers or moved before
the surrounding legacy source can be retired. No backward-compatibility guarantee
is made for older workflows.
