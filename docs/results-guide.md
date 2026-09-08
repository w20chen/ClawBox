# Read the results

The complete result directory is the evidence. A summary alone cannot establish correctness.

## Files

```text
prefix-provenance.json   source, selected rounds, policy order, deadline
replay-prefix.jsonl      selected rounds and labeled stop
replay-full.jsonl        unchanged source copy for --full-trace
plans/                  exact YAML for each baseline
00.log, 01.log, ...      worker output
arms/                   finished baseline attempt records
summary.json            combined results
report.md               readable table
runs/<run-id>/          detailed worker evidence
```

Only one replay file is used. Within a worker directory, `events/` contains lifecycle, admission, and memory events; `model-gateway/` contains replay checks; `policy-control/` contains tool control records; and `tool-artifacts/` contains cgroup, eBPF, and validation evidence. `owned-sandboxes.jsonl` records VM ownership.

## Correctness comes first

A passing baseline needs 40/40 completed sessions and successful validation. Check replay matching, exact execution-ID joins, and lost telemetry events. Missing measurements are not zero.

In the report, JCT is the time from an agent's arrival to its completion. P50 is the median and P95 is the 95th percentile. LOCAL GiB-s is memory use integrated over time; ID join is the fraction of tool records linked by their exact execution IDs.

A timeout is incomplete evidence. A live memory-sampling loop does not prove tool progress. The status command shows the age of the latest non-memory event.

Five rounds cover only early exploration. Separate post-run pytest checks the resulting workspace, but does not turn the prefix into a complete coding-task evaluation. Do not compare five-round, 23-round, and full-trace timings as if they were the same workload.

## Memory and timing

VM RAM sizes are fixed capacities. Per-tool reservations are host scheduling decisions, not VM resizing. LOCAL includes charged memory, overhead, and cache. Requested reclamation bytes are not assumed to have been freed.

The two tiered policies have extra WARM capacity. Without capacity-matched controls, improvements cannot be attributed solely to policy quality. The NUMA setup is not a measurement of a real multi-host CXL fabric. A single repetition does not estimate run-to-run uncertainty.

## Keep evidence outside Git

After a study finishes:

```bash
tar -czf check-01-results.tar.gz -C /data/clawbox-results check-01
```

Copy the archive to research storage. Do not delete active output, replace failed data with another run's data, or commit large logs and temporary bundles to GitHub.
