# Supported workflow and deprecated modules

Use `clawbox experiment` for new deployments and experiments. Its CubeSandbox
execution, ClawTune integration, native tuning, replay gateway, and result
collection remain supported.

The former service-based control plane is deprecated: `clawbox.api`, `managed`,
`scheduler`, `controller`, `allocator`, `node_agent`, `tool_agent`, `ingester`, and
the Cell orchestration in `cell`. Their source is retained for reference, not as
supported deployment entry points. Future changes need not preserve these
modules' behavior, historical APIs, schemas, aliases, or stored-state formats.
Do not deploy these services for the current experiment workflow.

The experiment policy catalog also retains superseded research baselines so old
result files remain interpretable. Only `tool-static-resident` (A),
`tool-p50-resident` (A+B), and `tool-p50-wait-reactive` (A+B+C) are active.
`clawbox experiment baselines --all` shows every retained policy with an explicit
`DEPRECATED` marker. Deprecated policies are excluded from new CLI selections and
future work does not preserve their behavior.

This does not deprecate shared functionality still used by current commands:

- `cell.p90` prediction export and its admission prediction data contract remain
  in use by current training/export commands.
- `replay` provides the active model gateway, trace reader, and execution types.
- `common` contains types and database support still used by native tuning.
- `benchmark` and placement helpers are auxiliary tools, not replacements for
  the experiment entry point.

These dependencies must be maintained for their current callers or moved before
the surrounding legacy source can be retired. No backward-compatibility guarantee
is made for older workflows.
