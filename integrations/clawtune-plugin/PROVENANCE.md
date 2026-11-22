Derived from ClawTune `packages/clawtune-plugin` at
`1b119259c06ac95cead9e5edd5e062ff57499343`, under the accompanying MIT license.

ClawBox carries the host prediction receipt in its SSH execution envelope and
blocks execution when `requireHostPrediction` is enabled and prediction fails.
The receipt comes from the decision's `placement.host_kb` control metadata;
the upstream three-branch prediction and public signals contain no receipt.
Unified observations and Session tracking follow this upstream revision.
The awaited `before_agent_start` hook registers logical Session ownership with
ClawBox before returning. Tool/model observations and child-spawn notifications
also register idempotently; observation-only model hooks are not execution gates.
The upstream ClawTune checkout is an unchanged dependency.
