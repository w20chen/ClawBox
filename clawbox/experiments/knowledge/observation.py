"""Project finalized Tool evidence using the pinned ClawTune ingestion rules."""
from dataclasses import asdict, replace
from tool_resource.sdk import _observations_from_call, _impute_execution_metrics, clause_cpu_time_ns
from tool_resource.runtime_kb import CompletedCall, training_target_provenance
from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds, _quality_gated_pmu_metrics
from clawtune_sidecar.monitoring.environment_memory import clause_memory_labels
from .protocol import PROTOCOL, command_digest, validate_message


def compact_observation(*, call, artifact, stats, repository, session_id, execution_id,
                        receipt, command, exit_code, timed_out=False, cancelled=False, tool_name='exec'):
    raw = call.get('clauses') or []
    rows = [_impute_execution_metrics(row) for row in
            _observations_from_call(repository, call, require_timestamps=False)]
    environment = stats if stats.get('memory_eligible') else {}
    labels = clause_memory_labels(environment, raw)
    combined = []
    for row, raw_row, memory in zip(rows, raw, labels):
        memory = dict(memory)
        source = environment if len(raw) == 1 else raw_row
        for name in ('memory_total_peak_bytes', 'memory_extra_peak_bytes'):
            if name not in memory and isinstance(source.get(name), (int, float)) and source[name] >= 0:
                memory[name] = source[name]
        missing = tuple(name for name in ('memory_total_peak_bytes', 'memory_extra_peak_bytes') if memory.get(name) is None)
        memory.update({name: 0 for name in missing})
        memory.setdefault('memory_measurement', stats.get('memory_measurement') or 'guest_memtotal_minus_memavailable')
        combined.append(replace(row, **memory, censored=row.censored or timed_out or cancelled,
            confirmed_timeout=row.confirmed_timeout or timed_out,
            imputed_targets=tuple(sorted(set(row.imputed_targets) | set(missing)))))
    profile = call.get('call_resource') or {}
    availability = profile.get('availability') or {}
    counters = [clause_cpu_time_ns(row) for row in raw]
    cpu = stats.get('cpu_time_seconds')
    cpu_eligible = stats.get('cpu_source') == 'cgroup-v2-cpu.stat' and stats.get('cgroup_attachment_verified') is True
    if cpu is None and counters and all(value is not None for value in counters):
        cpu = sum(counters) / 1e9
        cpu_eligible = True
    duration = float(stats['ts_end']) - float(stats['ts_start'])
    completed = CompletedCall(repository, tool_name, command, float(stats['ts_start']), float(stats['ts_end']),
        censored=timed_out or cancelled or call.get('censored') is True,
        confirmed_timeout=timed_out or call.get('confirmed_timeout') is True,
        timeout_duration_lower_bound_ms=(stats.get('duration_ms') if timed_out else call.get('timeout_duration_lower_bound_ms')),
        outcome='ok' if exit_code == 0 else 'error', workload_duration_required=True,
        workload_duration_seconds=_retained_workload_duration_seconds({'calls': [call]}),
        cpu_time_seconds=cpu, cpu_time_eligible=cpu is not None and cpu_eligible,
        cpu_avg_cores=cpu / duration if cpu is not None and duration > 0 else None,
        cpu_peak_cores=profile.get('peak_cpu_cores'), cpu_peak_window_ms=500 if profile.get('peak_cpu_cores') is not None else None,
        cpu_peak_cores_eligible=availability.get('cpu') == 'ok',
        sampled_peak_rss_bytes=(profile['sampled_peak_rss_mb'] * 1000000 if profile.get('sampled_peak_rss_mb') is not None else None),
        sampled_peak_rss_eligible=availability.get('memory') == 'ok',
        memory_baseline_bytes=environment.get('memory_baseline_bytes'),
        memory_total_peak_bytes=environment.get('memory_total_peak_bytes'),
        memory_extra_peak_bytes=environment.get('memory_extra_peak_bytes'),
        memory_measurement=stats.get('memory_measurement') or 'guest_memtotal_minus_memavailable',
        memory_environment_id=stats.get('memory_environment_id'), memory_eligible=bool(environment))
    pmu = _quality_gated_pmu_metrics(stats.get('pmu'), execution_id=execution_id)
    if pmu:
        completed = replace(completed, **{('pmu_eligible' if key == 'eligible' else 'pmu_' + key): value for key, value in pmu.items()})
    result = dict(schema_version=PROTOCOL, session_id=session_id, execution_id=execution_id,
        command=command, command_sha256=command_digest(command), repository=repository, tool_name=tool_name,
        memory_measurement='cgroup_v2_environment_union_v1', prediction_receipt=receipt,
        call=asdict(completed), clauses=[asdict(row) for row in combined],
        call_provenance=training_target_provenance(completed),
        quality=dict(collection_validity=artifact.get('collection_validity'), cleanup=artifact.get('cleanup'),
            loss_total=int((artifact.get('telemetry_loss_total') or {}).get('total') or 0),
            reason=None if call.get('eligible_for_kb') else 'partial_or_unavailable_collection'))
    validate_message(result, 'observe')
    return result
