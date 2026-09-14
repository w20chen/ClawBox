# Four-call placement audit

Run the offline checks from the repository root with the ClawBox environment:

```powershell
python -m clawbox.experiments.placement inspect --traces <trace-root> --output <output-dir>
python -m clawbox.experiments.placement topology --host kunpeng --output <output-dir>/topology.json
python -m clawbox.experiments.placement plan --calls <output-dir>/calls.json --output <output-dir>
```

`quality.json` records attribution, PMU coverage and replay exclusions. `calls.json` preserves the original tool calls. `plan.json` contains the task-level chronological split, historical prediction evidence, candidate funnel and any frozen P1/P2/P3 decisions. The planner never reads the final KB snapshot. Thresholds can be set with `--test-fraction`, `--min-active-seconds` and `--seed`.

Only run counterfactual trials after each candidate has a verified pre-call CubeSandbox checkpoint and the four actual host computation cores and NUMA memory placement have been checked. A trace export or a post-call workspace is not a checkpoint. Once verified paired rounds exist, summarize them with:

```powershell
python -m clawbox.experiments.placement report --plan <output-dir>/plan.json --rounds <rounds.json> --topology <output-dir>/topology.json --output <output-dir>/report.json
```

The reporter requires five paired rounds for P1, P2, P3 and the same four-core Linux pool. It keeps the placement selected in the frozen plan separate from the fastest placement observed afterward.

For the measured Docker/QEMU checkpoint experiment, copy `placement_docker.py`,
`placement_docker_all.py` and the frozen `plan.json` to `kunpeng` together. Run
two sacrificial warmups, then run the five randomized rounds:

```bash
python3 placement_docker.py --warmup
python3 -m placement_docker_all
```

The runner creates a fresh container from the target checkpoint for every
tool/placement/round, releases all four tools through a barrier, and stores the
timing, cgroup mapping, NUMA statistics, cgroup CPU accounting and `perf stat`
output under `PLACEMENT_FORMAL_ROOT`. A trial is accepted only when the target
exit code and stdout match the recorded call, the effective cpuset and NUMA
node match the plan, and all requested PMU events report 100% running time.

The checked-in follow-up result is described in `docs/placement-results.md`.
