# Auxiliary four-call placement analysis

This module analyzes an independent LLC-placement study. It is not part of the
`clawbox experiment` launch path. The current reporter assumes four calls
(A–D), a verified four-core pool, and NUMA 0 memory placement; its result cannot
be generalized to the two-compute-node supernode without changing those guards.

From the repository root, with the ClawBox Python environment active:

```bash
python -m clawbox.experiments.placement inspect --traces TRACE_ROOT --output OUT
python -m clawbox.experiments.placement topology --host HOST --output OUT/topology.json
python -m clawbox.experiments.placement plan --calls OUT/calls.json --output OUT
```

`inspect` writes `quality.json` for attribution and PMU coverage and
`calls.json` for original calls. `plan` freezes the chronological task split,
prediction evidence and P1/P2/P3 choice in `plan.json`; it does not read the
final KB snapshot. Adjust the split with `--test-fraction`, the call filter
with `--min-active-seconds`, and sampling with `--seed`.

A round submitted to `report` needs a verified pre-call checkpoint and
effective host CPU and NUMA mapping for each call. Preserve the command result
and PMU coverage separately when making a scientific claim. A trace export or
post-call workspace is not a checkpoint. After collecting at least five paired
rounds for P1, P2, P3 and the same Linux four-core control pool:

```bash
python -m clawbox.experiments.placement report \
  --plan OUT/plan.json --rounds ROUNDS.json \
  --topology OUT/topology.json --output OUT/report.json
```

The reporter checks the frozen plan hash and verified mapping. It keeps the
preselected placement separate from the fastest placement observed afterward.
The [historical result](archive/placement-2026.md) used Docker/QEMU checkpoints
on Kunpeng; it does not establish a native ARM or general NUMA-aware result.
