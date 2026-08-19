# P0 real-task acceptance — 2026-08-19

P0 is accepted on `weitianc@193.124.7.2`.

## Accepted run

- Run: `01M0CVS439CC4EFHK5SFE9Z013`
- SandboxTask: `run-01m0cvs439cc4efhk5sfe9z013-a1`
- Terminal state: `Cleaned`, outcome `Succeeded`, agent exit code `0`
- Final answer: 13,681 bytes
- Patch: 1,252 bytes, `patch_status=present`
- Tool image: `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:3e5803ff42a3b77151957f024df03e5e299cc856e5d74e200539a084c658b467`
- Runtime image at acceptance: `127.0.0.1:5000/clawbox/runtime-arm64:dev`

Strict `scripts/m1-p0-joincheck.sh` result:

- Bridge records: 189 total (`bridge-local=176`, `runtime-envelope=13`)
- Tool spans: 94
- DeepSeek LLM spans: 96
- Span execution IDs: 13
- Exact span-to-bridge matches: 13/13 (`join_rate=1.0`)
- Every joined span ID has `execution_source=runtime-envelope`

## Repairs included in this milestone

- Installed a narrow udev rule for `clawbox-fc--pool-snap-*` mappings so
  devmapper snapshot activation no longer stalls in filesystem probing.
- Added bounded, exact-target host recovery and live-capture helpers.
- Made result/join scripts select one Running ingester and fail closed on
  missing telemetry or artifacts.
- Added a singleton real-task watcher and bounded missing-CR exit.
- Rebuilt the runtime with the current ClawTune plugin schema.
- Rebuilt the immutable SWE task image with the current Tool Bridge.
- Made the bridge envelope shell-safe and compatible with OpenClaw's workdir
  wrapper while retaining backward compatibility with JSON envelopes.
- Preserved envelope instrumentation on sidecar decision fail-open paths.

## Validation

- ClawBox: 150 pytest tests passed.
- ClawTune plugin: 95 Node tests passed.
- Tool Bridge: Go test suite passed on native ARM64.

Unrelated untracked datasets, logs, and traces in the remote ClawTune checkout
were preserved and are not part of the milestone commit.
