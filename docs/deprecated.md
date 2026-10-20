# Supported workflow and deprecated modules

Use `clawbox experiment` for new deployments and experiments. CubeSandbox
execution, ClawTune predictions and measurements, replay, and result
collection remain supported.

The former service-based control plane is deprecated: `clawbox.api`, `managed`,
`scheduler`, `controller`, `allocator`, `node_agent`, `tool_agent`, `ingester`, and
orchestration in `cell`. Their source remains for reference, but these modules
are not deployment entry points and do not have compatibility guarantees.

The parallel `scripts/lab` command-line interface is also deprecated. Its shared
host setup and image helpers are still called by `clawbox experiment setup` and
`clawbox experiment images`; use the public experiment commands for new runs.

New experiments use schema version 4 and the recipes printed by
`clawbox experiment recipes`; see the [experiment guide](guide.md). Version 3
inputs and their suite/baseline selectors are not supported.

This does not deprecate shared functionality still used by current commands:

- `cell.p90` still exports prediction data for current training commands.
- `replay` provides the active model gateway, trace reader, and execution types.
- `common` contains types and database support still used by native tuning.
- `benchmark` and placement helpers are auxiliary tools, not replacements for
  the experiment entry point.

These dependencies must be maintained for their current callers or moved before
the surrounding legacy source can be retired. No backward-compatibility guarantee
is made for older workflows.
