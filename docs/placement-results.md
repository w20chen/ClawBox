# Placement experiment result

The experiment used four real calls reconstructed from immutable pre-call
Docker checkpoints: two Terminal-Bench `Rscript` calls (A/B) and two
SWE-ReBench `python` calls (C/D). The target commands, exit codes and stdout
were checked against the original trace. The containers used the original
Linux/amd64 images under QEMU on `kunpeng`; this is not a native ARM result.

The first five-round run used cores 0, 2, 8 and 10. It exposed external
contention on CPU 0: C or D occasionally took 16.6–21.3 seconds while its
cgroup CPU time was only about 9.5–12.2 seconds. Those rounds are retained as a
diagnostic result and are not used for the robust comparison.

The follow-up froze the plan before measuring any placement and used the same
two clusters and NUMA node with cores 2, 4, 8 and 10. Every one of the 20
placement runs used a fresh checkpoint-restored container. The four requested
PMU events (`LLC-loads`, `LLC-load-misses`, `cycles`, `instructions`) were
recorded with 100% running time on every accepted tool run; effective cpuset
and `cpuset.mems` matched the plan, and no run showed nonzero NUMA1 residency.

| placement | median makespan (s) | throughput (calls/s) | A (s) | B (s) | C (s) | D (s) |
|---|---:|---:|---:|---:|---:|---:|
| P1: AB / CD | 7.882 | 0.508 | 2.288 | 2.143 | 7.857 | 6.228 |
| P2: AC / BD | 7.855 | 0.509 | 2.155 | 2.043 | 7.830 | 6.213 |
| P3: AD / BC | 7.973 | 0.502 | 2.200 | 2.049 | 7.937 | 6.464 |
| Linux four-core pool | 8.195 | 0.488 | 2.093 | 2.082 | 8.181 | 6.279 |

The pre-execution PMU rule predicted A/B as high pressure (3.677M LLC reads per
perf-running second) and C/D as low pressure (1.619M). The follow-up measured
medians of 3.013M, 3.186M, 3.410M and 3.578M respectively, so this small
sample reversed the predicted ordering. LLC intensity therefore did not
predict placement sensitivity here.

The frozen predictor selected P2; the name-only predictor also selected P2 and
the fixed round-robin control was P1. P2 was only 0.027 seconds faster than P1
at the median and won 2 of 5 paired P1 comparisons. The tool-level placement
sensitivity `G_i` was 6.2% (A), 4.9% (B), 1.4% (C) and 4.0% (D); none reached
the predeclared 10% opportunity threshold. Thus this run finds no reliable
placement opportunity and no evidence that the LLC predictor improves the
choice. Linux default was 4.3% slower than P2 in median makespan, but its
paired difference was positive in only 4 of 5 rounds; this is a small effect,
not a claim of general scheduler superiority.

The complete frozen plan, raw rounds, PMU output and generated report are in
`.artifacts/placement-formal-v2/`. Re-run the offline validation with:

```powershell
python -m pytest tests/test_placement.py -q
python -m py_compile clawbox/experiments/placement.py clawbox/experiments/placement_docker.py clawbox/experiments/placement_docker_all.py
```
