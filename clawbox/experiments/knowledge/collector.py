"""ClawBox in-memory finalization, derived from pinned ClawTune telemetry."""
import ctypes
import json
from tool_resource.telemetry import (ClauseTelemetryCollector, _lifecycle_synchronized,
    _loss_counts, LOSS_COUNTER_NAMES, PAGE, SAMPLE_PERIOD_NS, WINDOW_NS, ALIGN_BIN_NS)


class HostClauseTelemetryCollector(ClauseTelemetryCollector):
    @_lifecycle_synchronized
    def finalize(self, *, replay_execution: str = "completed", write_artifact: bool = True) -> dict | None:
        if self.state == "closed":
            return
        if replay_execution not in {"completed", "failed", "incomplete"}:
            raise ValueError(f"invalid replay execution state {replay_execution!r}")
        try:
            current_loss_counts = (
                _loss_counts(self._bpf)
                if self.state == "active"
                else dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            )
            total_loss_counts = {
                name: max(
                    0,
                    int(current_loss_counts.get(name, 0))
                    - int(self._loss_baseline.get(name, 0)),
                )
                for name in LOSS_COUNTER_NAMES
            }
        except BaseException as exc:
            total_loss_counts = dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            self._disable(f"collector counter read failed: {type(exc).__name__}: {exc}")
        total_loss = sum(total_loss_counts.values())
        # Read diagnostic kprobe counter to determine if kprobes fired at all.
        try:
            kprobe_hits = (
                max(
                    0,
                    int(
                        self._bpf["kprobe_total_hits"][ctypes.c_int(0)].value
                    )
                    - self._kprobe_hits_baseline,
                )
                if self.state == "active" and hasattr(self, "_bpf")
                else 0
            )
        except BaseException:
            kprobe_hits = 0
        if self._active is not None:
            self._disable(
                f"unterminated exec delimiter: {self._active.tool_call_id}",
                tool_call_id=self._active.tool_call_id,
            )
            self._unavailable_call(self._active, reason="unterminated exec delimiter")
            self._active = None
        try:
            if hasattr(self, "_bpf"):
                self._close_bpf()
        except BaseException as exc:
            self._cleanup_status = "failed"
            self._disable(
                f"collector cleanup leak: {type(exc).__name__}: {exc}"
            )
        if total_loss:
            causes = ",".join(
                f"{name}={count}" for name, count in total_loss_counts.items() if count
            )
            self._integrity_errors.append(
                f"collector total telemetry loss={total_loss} ({causes})"
            )
        poll_error = self._shared_poll_error()
        if poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(poll_error).__name__}: {poll_error}"
            )
        prior_state = self.state
        valid_count = sum(
            call.get("telemetry_quality") == "ok" for call in self.calls
        )
        invalid_count = sum(
            call.get("telemetry_quality") == "invalid" for call in self.calls
        )
        unavailable_count = sum(
            call.get("telemetry_quality") == "unavailable" for call in self.calls
        )
        eligible_count = sum(
            call.get("eligible_for_kb") is True for call in self.calls
        )
        collector_healthy = (
            prior_state == "active"
            and self._cleanup_status == "ok"
            and total_loss == 0
            and unavailable_count == 0
        )
        if not collector_healthy:
            telemetry_quality = "unavailable"
        elif invalid_count:
            telemetry_quality = "invalid"
        else:
            telemetry_quality = "ok"
        formal_completeness = (
            "unavailable"
            if not collector_healthy
            else ("complete" if eligible_count == len(self.calls) else "partial")
        )
        collection_validity = (
            "valid"
            if collector_healthy and invalid_count == 0
            else "invalid"
        )
        call_errors = {
            str(error)
            for call in self.calls
            for error in (call.get("integrity") or {}).get("errors", [])
        }
        collector_errors = (
            [
                error
                for error in self._integrity_errors
                if error not in call_errors
            ]
            if collector_healthy
            else list(self._integrity_errors)
        )
        artifact = {
            "schema": "clause_telemetry_v2",
            "version": 2,
            "mode": "clause",
            "status_model": "call_granular_v1",
            "container_id": self.container_id,
            "cgroup_id": self.cgroup_id,
            "quota_cores": self.quota_cores,
            "calls": self.calls,
            "telemetry_loss_total": {
                **total_loss_counts,
                "total": total_loss,
            },
            "ring_loss_total": total_loss,
            "cleanup": self._cleanup_status,
            "collector": {
                "state": "closed",
                "state_before_close": prior_state,
                "health": (
                    "healthy" if collector_healthy else "unavailable"
                ),
                "first_disabled_call": self._first_disabled_call,
                "disabled_reason": self._disabled_reason,
                "valid_call_count": valid_count,
                "invalid_call_count": invalid_count,
                "unavailable_call_count": unavailable_count,
                "eligible_call_count": eligible_count,
                "kprobe_total_hits": kprobe_hits,
            },
            "call_coverage": {
                "total_call_count": len(self.calls),
                "eligible_call_count": eligible_count,
                "withheld_call_count": len(self.calls) - eligible_count,
                "eligible_fraction": (
                    eligible_count / len(self.calls) if self.calls else 1.0
                ),
            },
            "replay_execution": replay_execution,
            "telemetry_quality": telemetry_quality,
            "formal_completeness": formal_completeness,
            "collection_validity": collection_validity,
            "integrity": {
                "status": (
                    "ok"
                    if collector_healthy and invalid_count == 0
                    else "failed"
                ),
                "errors": collector_errors,
            },
            "provenance": {
                "collector": "ebpf_ebpf",
                "repo": self.repo,
                "page_size_bytes": PAGE,
                "cadence_ns": SAMPLE_PERIOD_NS,
                "window_ns": WINDOW_NS,
                "align_bin_ns": ALIGN_BIN_NS,
                "disk_io_semantics": (
                    "nonnegative_per_tid_linux_task_io_accounting_deltas"
                ),
                "disk_io_fields": [
                    "task->ioac.read_bytes",
                    "task->ioac.write_bytes",
                    "task->ioac.cancelled_write_bytes",
                ],
            },
        }
        if write_artifact:
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.state = "closed"
        return artifact
