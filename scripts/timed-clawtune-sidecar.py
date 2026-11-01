#!/usr/bin/env python3
"""Start the ClawTune sidecar while publishing guest-local startup phases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any


def _publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _time_host_predictions(predictor: Any) -> None:
    """Keep client round-trip timing in the already-collected sidecar log."""
    predict = predictor.predict

    def measured(request: Any) -> Any:
        if request.tool_name not in {"exec", "terminal_exec"}:
            return predict(request)
        started = time.monotonic()
        record = {
            "event": "host_kb_client_prediction",
            "event_id": request.event_id,
            "tool_call_id": request.tool_call_id,
            "status": "ok",
        }
        try:
            result = predict(request)
            record["remote_prediction"] = bool(
                (result.tool_resource or {}).get("host_kb")
            )
            return result
        except Exception as exc:
            record.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            record["seconds"] = time.monotonic() - started
            print(json.dumps(record, separators=(",", ":")), flush=True)

    predictor.predict = measured


def main() -> None:
    process_entry = time.monotonic()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timing-output", type=Path, required=True)
    args = parser.parse_args()
    timing: dict[str, Any] = {
        "schema": "clawtune_sidecar_startup_v1",
        "status": "importing",
    }
    _publish(args.timing_output, timing)
    try:
        import_started = time.monotonic()
        import uvicorn
        from clawtune_sidecar.api.app import create_app
        if os.environ.get('CLAWTUNE_HOST_KB_ENDPOINT'):
            from clawbox.experiments.knowledge.runtime_state import build_state
        else:
            from clawtune_sidecar.api.dependencies import build_state
        imported = time.monotonic()
        timing.update({
            "process_entry_to_import_start_seconds": import_started - process_entry,
            "module_import_seconds": imported - import_started,
            "status": "building_state",
        })
        _publish(args.timing_output, timing)

        state_started = time.monotonic()
        state = build_state()
        if os.environ.get('CLAWTUNE_HOST_KB_ENDPOINT'):
            _time_host_predictions(state.predictor)
        state_built = time.monotonic()
        timing.update({
            "build_state_seconds": state_built - state_started,
            "status": "constructing_app",
        })
        _publish(args.timing_output, timing)

        prepare = state.tool_monitor.prepare

        def timed_prepare() -> None:
            started = time.monotonic()
            try:
                prepare()
            finally:
                timing.update({
                    "collector_prepare_seconds": time.monotonic() - started,
                    "process_entry_to_collector_prepared_seconds": (
                        time.monotonic() - process_entry
                    ),
                    "status": "collector_prepared",
                })
                _publish(args.timing_output, timing)

        state.tool_monitor.prepare = timed_prepare
        app_started = time.monotonic()
        app = create_app(state)
        timing.update({
            "app_construction_seconds": time.monotonic() - app_started,
            "status": "starting_server",
        })
        _publish(args.timing_output, timing)
        uvicorn.run(app, host=args.host, port=args.port)
    except BaseException as exc:
        timing.update({
            "status": "failed",
            "failure_type": type(exc).__name__,
            "process_entry_to_failure_seconds": time.monotonic() - process_entry,
        })
        _publish(args.timing_output, timing)
        raise


if __name__ == "__main__":
    main()
