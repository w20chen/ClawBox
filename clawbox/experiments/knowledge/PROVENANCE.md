# ClawTune integration source

Upstream: https://github.com/w20chen/ClawTune
Revision: `4a6d0af66e76b377bfc957baf84a5f414c6d8bf5`
License: MIT; see LICENSE.

ClawTune is an unmodified dependency. ClawBox owns the host protocol and all
deployment adaptations in this directory.

- `incremental.py` derives from `tool_time/incremental.py`: private layered
  containers replace seed-sized copies; statistical formulas remain upstream.
- `collector.py` derives from `ClauseTelemetryCollector.finalize` in
  `tool_resource/telemetry.py`: returns the finalized object and optionally
  writes it. Collection and measurement remain upstream.
- `runtime_state.py` derives from `clawtune_sidecar/api/dependencies.py`: selects
  the remote adapter before constructing any local KB.
- `scripts/clawbox-guest-collector.py` derives from upstream
  `tools/guest_collector_server.py` and uses the ClawBox finalization adapter.

`integrations/clawtune-plugin` is the ClawBox-delivered plugin derived from the
same revision. Its changes carry opaque host receipts and enforce required
host predictions. It does not modify OpenClaw core or the ClawTune checkout.
