# CubeSandbox PMU validation

CubeSandbox Runtime and Tool images must be built with the sibling ClawTune
checkout as a named BuildKit context, so both Docker and Cube delivery consume
the same collector source:

```bash
docker buildx build --build-context clawtune=../ClawTune \
  -f docker/Dockerfile.runtime-cube -t clawbox/runtime-cube:pmu .
docker buildx build --build-context clawtune=../ClawTune \
  -f docker/Dockerfile.tool-cube -t clawbox/tool-cube:pmu .
```

The Tool VM must expose a vPMU and allow `perf_event_open`. The Kubernetes Tool
manifest requests `PERFMON`; inspect the guest health response and retained
`pmu-profile-*.json` artifacts rather than assuming capability implies event
support.

Before a production run, execute the sibling ClawTune acceptance check on the
same kernel/architecture, then run the ordinary ClawBox single-session and
multi-session experiments with PMU enabled and disabled. Compare host
throughput, Tool median/p95 latency, and the distribution of
`coverage.status`, `coverage.reason`, and `coverage.running_ratio`. On Kunpeng,
also verify each profile names exactly `PERF_COUNT_HW_CACHE_LL:READ:ACCESS` and
`PERF_COUNT_HW_CACHE_LL:READ:MISS`; reject a result labeled with generic cache
misses or `hisi_l3c`.

PMU collection is best effort. A missing standalone PMU artifact or a profile
whose quality is not `reliable` must not fail the command and must not train the
online KB.

ClawBox checks the execution ID, event support, kernel coverage, and counter
running times before accepting a profile as reliable. Invalid PMU data is
excluded while valid cgroup CPU and memory accounting is retained. A degraded
Tool artifact also clears any older span-side PMU metrics. Reliable metrics
are forwarded to the native ClawTune `runtime_tool_resource_kb_v2` targets
`pmu_ipc`, `pmu_llc_mpki`, and `pmu_llc_miss_rate`.
