# ClawTune integration source

Upstream: https://github.com/w20chen/ClawTune
Revision: `1b119259c06ac95cead9e5edd5e062ff57499343`
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
- `session_forecast.py` subclasses the pinned Session state manager. Host-live
  forecasts use ClawBox admission evidence and Tool bridge start notifications;
  recovered snapshots without original completion times do not train handoff
  latency. The upstream JSON signal schema remains unchanged.
  Its authenticated Session-registration route protects shared Runtime residency.
- `remote.py` and the host prediction contract carry logical gateway/runtime/
  Session/run identity, separating invocation receipts inside the shared pair KB.
- `scripts/clawbox-guest-collector.py` derives from upstream
  `tools/guest_collector_server.py` and uses the ClawBox finalization adapter.

`integrations/clawtune-plugin` is the ClawBox-delivered plugin derived from the
same revision. Its changes carry opaque host receipts and enforce required
host predictions. It does not modify OpenClaw core or the ClawTune checkout.
