# Four-call CPU placement analysis

These commands analyze a separate last-level-cache (LLC) placement study.
They are outside the `clawbox experiment` workflow. The report command
requires four calls (A–D), a verified four-core CPU pool, and NUMA 0 memory
placement. Those fixed assumptions must be changed before using it for the
two-compute-node experiment.

From the repository root, with the ClawBox Python environment active, use an
SSH hostname reachable without an interactive password for `--host`:

```bash
python -m clawbox.experiments.placement inspect --traces TRACE_ROOT --output OUT
python -m clawbox.experiments.placement topology --host HOST --output OUT/topology.json
python -m clawbox.experiments.placement plan --calls OUT/calls.json --output OUT
```

`inspect` writes `quality.json` with attribution and performance-counter
coverage, and `calls.json` with the original calls. `plan` records the
chronological training/test split, prior prediction evidence, and a selected
CPU mapping in `plan.json`. It does not use prediction state produced after
the measured calls. Adjust the split with `--test-fraction`, the minimum
call duration with `--min-active-seconds`, and sampling with `--seed`.

A round submitted to `report` needs a verified pre-call checkpoint and
effective host CPU and NUMA mapping for each call. Preserve the command result
and PMU coverage separately when making a scientific claim. A trace export or
post-call workspace is not a checkpoint. After collecting at least five paired
rounds for each of the three candidate mappings (P1, P2, P3) and the same
Linux four-core control pool:

```bash
python -m clawbox.experiments.placement report \
  --plan OUT/plan.json --rounds ROUNDS.json \
  --topology OUT/topology.json --output OUT/report.json
```

The report checks the frozen plan hash and verified mapping. It keeps the
mapping selected before measurement separate from the fastest mapping found
afterward.
The [historical result](archive/placement-2026.md) used Docker/QEMU checkpoints
on Kunpeng; it does not establish a native ARM or general NUMA-aware result.
