# CubeSandbox handoff (2026-09-03)

## Current verified state

The Kunpeng cluster is healthy and CubeSandbox is deployed as Helm release
`cube` in `cube-system`. Pin: `v0.7.0`, commit
`d0081641c59822e4e5653b7462e914410b81910a`; Python SDK `0.7.0` with its
compatible undeclared command dependency `e2b==2.29.5`.

Final template: `tpl-c7212cdc724844639aa65486`, alias
`clawbox-task-arm64-4g`, ARM64 image digest
`sha256:e1cb43e12ba70b8453b45f0c063306faab8a6974aa3fd76982dc4d019d07c60d`.

Immutable images currently deployed:

- Worker: `127.0.0.1:5000/clawbox/experiment-worker@sha256:9cdb4634a14b962e0c1e8214cf8c97d3fb356c40652258f645aedf810861a9d2`
- Controller: `127.0.0.1:5000/clawbox/control-plane-arm64@sha256:8696240154448a63cfb3d7f42aac20299fa733b9cef1352482fb82fd09ba6787`

`deploy/sandboxtask-vertical-slice.yaml` succeeded. Result:
`/data/clawbox-results/k8s-vertical-slice-v2/attempt-1/k8s-vertical-slice-v2/summary.json`.
It completed one Agent, one pause and one resume, validation passed, and emitted
JSON, JSONL, CSV, and Markdown. The direct matrix at
`/home/weitianc/clawbox-results/real-matrix-1` passed four Agents for both
`static-resident` and `eager-reactive` (eight successful sessions total).

Cancellation fixture `cube-cancel-smoke` reached `cleaned`; its Job disappeared.
A controller rollout left exactly one Job for the successful task. Cube API
listed zero sandboxes after each gate. The lifecycle smoke measured roughly
598 MiB more MemAvailable while paused and preserved process/file state.

Local gate:

```text
python -m pytest -q --basetemp .pytest-tmp-root-7
# all collected tests passed; 5 skipped
```

All nine Helm test hooks reported `Succeeded` at 2026-09-03 10:58-10:59 UTC.

## Important fixes in this continuation

- preserve chart-templated DB/Redis hosts while applying fixed research ratios;
- expose envd ports on registered templates;
- make `controlPlane.api.sandboxDomain` match `cubeProxy.domain`;
- add CoreDNS wildcard rewrite with `answer auto`;
- chown the dedicated result hostPath in a capability-limited init container;
- rerun failed arms instead of trusting their completion marker;
- add SandboxTask UID, resolved template/image provenance, cluster versions and
  readable Markdown to results;
- replace stale v1 tests/docs and retire old public console entry points.

## Remaining work

The real OpenClaw driver is not implemented. The current worker should not be
credited with OpenClaw execution merely because the enum exists: its action
path is trace-based. Build a trusted Worker-side OpenClaw integration whose
tool calls use `CubeCommandExecutor`, install OpenClaw in the Worker image, and
run it with real model credentials before claiming acceptance.

Old Kata/direct-Firecracker/SSH implementation modules and some migration
scripts still exist in git although they are no longer console entry points,
documented paths, or active RBAC/deployment paths. Delete them in a focused
follow-up after checking whether any retained tuning/data utilities import
them. Result telemetry still lacks precise paused-duration, peak concurrent
resident count, and proactive hit/miss counters.

The supplied BoostKit irqbypass/XArray kernel change remains optional and was
not applied. Do not change or reboot the host kernel without explicit approval.
