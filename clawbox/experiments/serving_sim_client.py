"""Host-owned model streams and one shared GPU observation stream per experiment arm."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from clawbox.replay.process_sessions import rebind_process_sessions
from .serving_sim_diagnostics import Diagnostics, exception_record
from .spec import ServingSimLaunchSpec


CPU_FEEDBACK_PATH = "/v1/clawbox/simulator/cpu-feedback"


def serving_sim_bases(configuration: dict) -> list[str]:
    """Expand one managed base port into the configured independent clusters."""
    raw = str(configuration["base_url"]).rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    parsed = urlsplit(raw)
    count = int((configuration.get("serving_sim_launch") or {}).get(
        "cluster_count", 1,
    ))
    port = parsed.port or 80
    return [
        urlunsplit((parsed.scheme, f"{parsed.hostname}:{port + index}", "", "", ""))
        for index in range(count)
    ]


def assign_trace_clusters(case_bindings: dict, cluster_count: int) -> dict[str, int]:
    """Assign each unique trace once while preserving deterministic case order."""
    assignments = {}
    for case_id in sorted(case_bindings):
        trace_id = case_bindings[case_id]['trace_id']
        if trace_id not in assignments:
            assignments[trace_id] = len(assignments) % cluster_count
    return assignments


class _Message:
    """Retain output text once, not one JSON envelope per simulated token."""

    def __init__(self):
        self.text = {}
        self.tools = {}
        self.role = 'assistant'

    def append(self, delta):
        self.role = delta.get('role', self.role)
        for key in ('content', 'reasoning', 'reasoning_content'):
            value = delta.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise RuntimeError(f'simulator {key} delta must be text')
                self.text.setdefault(key, []).append(value)
        for item in delta.get('tool_calls') or []:
            target = self.tools.setdefault(item['index'], dict(id=[], type='function', name=[], arguments=[]))
            if item.get('id'):
                target['id'].append(item['id'])
            if item.get('type'):
                target['type'] = item['type']
            for key, value in (item.get('function') or {}).items():
                if key in ('name', 'arguments') and value is not None:
                    target[key].append(value)

    def value(self):
        result = dict(role=self.role, **{k: ''.join(v) for k, v in self.text.items()})
        if self.tools:
            result['tool_calls'] = [dict(index=i, id=''.join(t['id']), type=t['type'],
                function=dict(name=''.join(t['name']), arguments=''.join(t['arguments'])))
                for i, t in sorted(self.tools.items())]
        return result


async def sse_events(response):
    data, event, identity = [], "message", ""
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, identity, "\n".join(data)
            data, event, identity = [], "message", ""
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("id:"):
            identity = line[3:].strip()
    if data:
        raise RuntimeError("simulator SSE ended in an incomplete event")


class ServingSimClient:
    def __init__(self, configuration: dict, credential: str, output: Path):
        self.configuration = configuration
        self.bases = serving_sim_bases(configuration)
        self.base = self.bases[0]
        self.credential, self.output = credential, output
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="serving-sim", daemon=True)
        self.callbacks = {}
        self.requests = {}
        self.cancelled_requests = set()
        self.error = None
        self.stopping = False
        self.compact_diagnostics = configuration.get('serving_sim_diagnostic_detail') == 'compact'
        self.feedback_lock = threading.Lock()
        self.feedback_pending = None
        self.feedback_sending = False
        self.feedback_future = None
        self.feedback_sequence = 0
        self.feedback_next_sample_monotonic = float('-inf')
        self.feedback_tasks = set()
        self.feedback_submitted = 0
        self.feedback_coalesced = 0

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(30)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    async def _start(self):
        self.diagnostics = Diagnostics(self.output / 'requests.jsonl')
        self.sample_task = asyncio.create_task(self.diagnostics.sample_loop(
            float(self.configuration.get('serving_sim_diagnostic_interval_seconds', 1.))))
        self.client = httpx.AsyncClient(trust_env=False,
            headers={"Authorization": "Bearer " + self.credential} if self.credential else {},
            timeout=httpx.Timeout(20, read=None),
            # Uvicorn closes idle sockets after 5 s. Expire ours first to avoid
            # racing that close (the native load client uses the same 2 s bound).
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=32,
                keepalive_expiry=float(self.configuration.get('serving_sim_keepalive_expiry_seconds', 2.))))
        from .serving_sim_inputs import load_serving_sim_manifest
        manifest = load_serving_sim_manifest(Path(self.configuration["serving_sim_catalog_manifest"]), verify=True)
        self.case_bindings = {row['case_id']: row for row in manifest['cases']}
        self.infos = []
        self.clock_offsets_s = []
        self.clock_uncertainties_s = []
        expected_topology = self.configuration.get("serving_sim_topology_digest")
        launch = ServingSimLaunchSpec.model_validate(
            self.configuration["serving_sim_launch"]
        )
        expected_cpu_refill = launch.cpu_refill_configuration()
        for cluster_id, base in enumerate(self.bases):
            sent = time.time()
            response = await self.client.get(
                base + "/v1/clawbox/simulator/info", timeout=20,
            )
            received = time.time()
            response.raise_for_status()
            info = response.json()
            if info.get('adapter_protocol') != 3:
                raise ValueError(
                    f'serving_sim cluster {cluster_id} requires ClawBox adapter protocol 3; restart with the current ClawBox serve command'
                )
            forecast = info.get('response_forecast', {})
            expected_forecast = dict(protocol=1, interval_seconds=launch.forecast_interval_seconds,
                window_seconds=launch.forecast_window_seconds, validity_seconds=launch.forecast_validity_seconds,
                target='native_response_ready', output_length_source='recorded_current_call')
            if forecast != expected_forecast:
                raise ValueError(f'serving_sim cluster {cluster_id} response forecast configuration differs from experiment')
            offset = (sent + received) / 2 - float(info["server_wall_time"])
            uncertainty = max(0., received - sent) / 2
            info["client_clock_alignment"] = dict(
                offset_s=offset, uncertainty_s=uncertainty,
                method="HTTP request midpoint",
            )
            if not info.get("ready") or info.get("clock", {}).get("k") != 1:
                raise ValueError(
                    f"serving_sim cluster {cluster_id} requires a ready k=1 service"
                )
            if info.get('prompt_source') != 'actual':
                raise ValueError(
                    f'serving_sim cluster {cluster_id} requires actual Agent prompts'
                )
            if type(info.get("accepted_requests")) is not int or info["accepted_requests"] != 0:
                raise ValueError(
                    f"serving_sim cluster {cluster_id} already contains requests; "
                    "restart it with a new output directory for this arm"
                )
            if expected_topology is not None and info.get("topology_digest") != expected_topology:
                raise ValueError(
                    f"serving_sim cluster {cluster_id} topology differs from the experiment"
                )
            if info.get("catalog_digest") != manifest["catalog"]["catalog_digest"]:
                raise ValueError(
                    f"serving_sim cluster {cluster_id} catalog differs from the CPU manifest"
                )
            if (info.get('cpu_refill') or {}).get('config') != expected_cpu_refill:
                raise ValueError(
                    f'serving_sim cluster {cluster_id} cpu_refill configuration differs '
                    'from the experiment'
                )
            for row in self.case_bindings.values():
                actual = info.get('traces', {}).get(row['trace_id'], {})
                if any(actual.get(k) != row[k]
                       for k in ('model_calls', 'missing_reasoning_calls')):
                    raise ValueError(
                        f"{row['case_id']}: simulator cluster {cluster_id} trace counts "
                        "differ from the CPU manifest; prepare a new bundle"
                    )
            self.infos.append(info)
            self.clock_offsets_s.append(offset)
            self.clock_uncertainties_s.append(uncertainty)
        self.info = self.infos[0]
        self.clock_offset_s = self.clock_offsets_s[0]
        self.clock_uncertainty_s = self.clock_uncertainties_s[0]
        self.epochs = [info["service_id"] for info in self.infos]
        self.epoch = self.epochs[0]
        self.feedback_enabled = expected_cpu_refill['mode'] != 'off'
        self.feedback_interval_seconds = expected_cpu_refill[
            'feedback_interval_seconds'
        ]
        self.trace_clusters = assign_trace_clusters(
            self.case_bindings, len(self.bases),
        )
        service_document = (
            self.info if len(self.infos) == 1 else {
                "adapter": "clawbox-pool",
                "cluster_count": len(self.infos),
                "routing": "sticky_balanced_by_case",
                "clusters": [
                    {**info, "cluster_id": index, "base_url": self.bases[index]}
                    for index, info in enumerate(self.infos)
                ],
            }
        )
        (self.output / "service.json").write_text(
            json.dumps(service_document, indent=2), encoding="utf-8",
        )
        self.signal_log = (self.output / "observations.jsonl").open("a", encoding="utf-8")
        self.signal_ready = [asyncio.Event() for _ in self.bases]
        self.signal_tasks = [
            asyncio.create_task(self._signals(cluster_id))
            for cluster_id in range(len(self.bases))
        ]
        self.signal_task = self.signal_tasks[0]
        await asyncio.wait_for(asyncio.gather(*(
            ready.wait() for ready in self.signal_ready
        )), 20)
        self.check()

    async def _signals(self, cluster_id: int):
        base = self.bases[cluster_id]
        info = self.infos[cluster_id]
        epoch = self.epochs[cluster_id]
        try:
            cursor = info["signals"]["head"]
            async with self.client.stream("GET", base + "/request-signals/clawbox/events",
                    params={"after": cursor}) as response:
                response.raise_for_status()
                self.signal_ready[cluster_id].set()
                async for kind, identity, data in sse_events(response):
                    if kind == "gap":
                        raise RuntimeError("simulator signal history has a gap; the signal-driven arm cannot continue")
                    record = json.loads(data)
                    if not identity.startswith(epoch + ":"):
                        raise RuntimeError("simulator signal epoch changed")
                    received = time.time()
                    record.update(cluster_id=cluster_id,
                                  signal_sequence=int(identity.rsplit(":", 1)[1]),
                                  received_unix_s=received,
                                  signal_age_seconds=max(0., received - float(record["wall_t"])
                                      - self.clock_offsets_s[cluster_id]
                                      + self.clock_uncertainties_s[cluster_id]))
                    binding = self.callbacks.get(record.get("request_id"))
                    callback_started = time.monotonic()
                    try:
                        if binding:
                            session_id, callback = binding
                            if record.get("session_id") != session_id:
                                raise RuntimeError("simulator signal session identity mismatch")
                            if callback is not None:
                                callback(record)
                    finally:
                        record['callback_seconds'] = time.monotonic() - callback_started
                        log_signal = bool(record.pop("_clawbox_log_signal", True))
                        suppressed = record.pop(
                            "_clawbox_queued_observations_suppressed", 0,
                        )
                        logged = ({k: record[k] for k in (
                            'event', 'session_id', 'request_id', 'cluster_id', 'engine_id', 'role', 'source',
                            'signal_sequence', 't', 'wall_t', 'queued_since', 'est_wait_s', 'layer',
                            'forecast_protocol', 'phase', 'remaining_s', 'valid_for_s', 'unavailable_reason',
                            'target', 'output_length_source', 'output_tokens', 'rate_s_per_token',
                            'adapter_emit_unix_s', 'received_unix_s', 'signal_age_seconds', 'callback_seconds')
                            if k in record} if self.compact_diagnostics else record)
                        if log_signal:
                            if suppressed:
                                logged["queued_observations_suppressed"] = int(suppressed)
                            self.signal_log.write(
                                json.dumps(logged, separators=(",", ":")) + "\n"
                            )
                if not self.stopping:
                    raise RuntimeError("simulator signal stream disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            self.diagnostics.write(
                'signal_failure', cluster_id=cluster_id, error=exception_record(exc),
            )
            for task in list(self.requests.values()):
                task.cancel()
        finally:
            self.signal_ready[cluster_id].set()

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"serving_sim observation failed: {self.error}") from self.error

    def publish_cpu_feedback(self, *, run_id: str, attempt_id: str, arm_id: str,
                             sample_monotonic_s: float, cpu: dict,
                             activity: dict, admission: dict) -> bool:
        """Queue a latest-only aggregate snapshot without blocking the sampler."""
        self.check()
        if not self.feedback_enabled or cpu.get('average_cores') is None:
            return False
        sample_monotonic_s = float(sample_monotonic_s)
        with self.feedback_lock:
            if sample_monotonic_s < self.feedback_next_sample_monotonic:
                return False
            self.feedback_next_sample_monotonic = (
                sample_monotonic_s + self.feedback_interval_seconds
            )
            payload = dict(
                run_id=run_id, attempt_id=attempt_id, arm_id=arm_id,
                sample_monotonic_s=sample_monotonic_s,
                sample_unix_s=time.time() - max(
                    0., time.monotonic() - sample_monotonic_s,
                ),
                capacity_cores=float(cpu['capacity_cores']),
                average_cores=float(cpu['average_cores']),
                utilization_fraction=float(cpu['utilization_fraction']),
                ready_tool_operations=int(activity.get(
                    'tool_admitted_waiting_start_operations', 0,
                )),
                resource_blocked_tool_operations=int(
                    activity.get('tool_admission_waiting_operations', 0)
                    + activity.get('dependency_install_waiting_operations', 0)
                ),
                tool_executing_operations=int(activity.get(
                    'tool_executing_operations', 0,
                )),
                idle_at_llm_sessions=int(activity.get('idle_at_llm_sessions', 0)),
                activity_measurement=activity.get('measurement'),
                admission_measurement=admission.get('measurement'),
                admission_queue_depth=admission.get('admission_queue_depth'),
            )
            if self.feedback_sending:
                if self.feedback_pending is not None:
                    self.feedback_coalesced += 1
                self.feedback_pending = payload
                return True
            self.feedback_sending = True
        self._start_feedback(payload)
        return True

    def _start_feedback(self, payload: dict) -> None:
        with self.feedback_lock:
            sequence = self.feedback_sequence
            self.feedback_sequence += 1
            self.feedback_submitted += 1
        future = asyncio.run_coroutine_threadsafe(
            self._publish_cpu_feedback(dict(payload, sequence=sequence)), self.loop,
        )
        with self.feedback_lock:
            self.feedback_future = future
        future.add_done_callback(self._feedback_done)

    def _feedback_done(self, future) -> None:
        error = None
        try:
            future.result()
        except concurrent.futures.CancelledError:
            if not self.stopping:
                error = RuntimeError('cpu feedback delivery was cancelled')
        except BaseException as exc:
            error = exc
        next_payload = None
        with self.feedback_lock:
            self.feedback_future = None
            self.feedback_sending = False
            if error is None and not self.stopping and self.feedback_pending is not None:
                next_payload = self.feedback_pending
                self.feedback_pending = None
                self.feedback_sending = True
            elif error is not None:
                self.feedback_pending = None
        if error is not None:
            if self.error is None:
                self.error = RuntimeError(f'cpu feedback delivery failed: {error}')
            self.diagnostics.write(
                'cpu_feedback_failure', error=exception_record(error),
            )
            self.loop.call_soon_threadsafe(
                lambda: [task.cancel() for task in list(self.requests.values())]
            )
        elif next_payload is not None:
            self._start_feedback(next_payload)

    async def _publish_cpu_feedback(self, payload: dict) -> None:
        task = asyncio.current_task()
        self.feedback_tasks.add(task)
        try:
            async def deliver(cluster_id: int, base: str):
                body = dict(payload, cluster_id=cluster_id,
                            service_id=self.epochs[cluster_id])
                response = await self.client.post(
                    base + CPU_FEEDBACK_PATH, json=body, timeout=10,
                )
                response.raise_for_status()
                result = response.json()
                if (result.get('accepted') is not True
                        or result.get('sequence') != payload['sequence']):
                    raise RuntimeError(
                        f'serving_sim cluster {cluster_id} returned an invalid '
                        'cpu feedback acknowledgement'
                    )
                return dict(cluster_id=cluster_id, active=result.get('active'),
                            feedback_age_seconds=result.get('feedback_age_seconds'))
            states = await asyncio.gather(*(
                deliver(cluster_id, base)
                for cluster_id, base in enumerate(self.bases)
            ))
            self.diagnostics.write(
                'cpu_feedback_delivered', sequence=payload['sequence'],
                sample_monotonic_s=payload['sample_monotonic_s'], clusters=states,
            )
        finally:
            self.feedback_tasks.discard(task)

    def submit(self, *, session_id: str, request_id: str, trace_id: str, call_id: int,
               payload: dict, on_signal: Callable | None, timeout_s: float):
        self.check()
        return asyncio.run_coroutine_threadsafe(self._complete(
            session_id, request_id, trace_id, call_id, payload, on_signal, timeout_s), self.loop)

    def complete(self, *, timeout_s: float, **request):
        future = self.submit(timeout_s=timeout_s, **request)
        try:
            return future.result(timeout_s + 1)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("serving_sim model request timed out; GPU stream cancelled") from None
        except concurrent.futures.CancelledError:
            self.check()
            raise RuntimeError("serving_sim model request cancelled") from None

    async def _complete(self, sid, rid, trace_id, call_id, payload, on_signal, timeout_s):
        if rid in self.cancelled_requests:
            raise asyncio.CancelledError()
        self.check()
        self.callbacks[rid] = sid, on_signal
        self.requests[rid] = asyncio.current_task()
        expected = dict(trace_id=trace_id, call_id=call_id)
        cluster_id = self.trace_clusters.get(trace_id)
        if cluster_id is None:
            raise RuntimeError(f"serving_sim trace has no cluster assignment: {trace_id}")
        base = self.bases[cluster_id]
        forwarded = {**payload, "stream": True, "session_id": sid, "request_id": rid,
                     "replay": expected, "model": self.configuration["model"]}
        message, finished, done, first = _Message(), False, False, None
        mapping = None
        diagnostic = dict(session_id=sid, request_id=rid, trace_id=trace_id, call_id=call_id,
                          cluster_id=cluster_id,
                          stage='submit', times={'submit': time.time()})
        self.diagnostics.write('request_submitted', **diagnostic)
        def stage(name):
            diagnostic['stage'] = name
            diagnostic['times'][name] = time.time()
        try:
            async with asyncio.timeout(timeout_s):
                async with self.client.stream("POST", base + "/v1/chat/completions", json=forwarded) as response:
                    stage('response_headers')
                    diagnostic['http_status'] = response.status_code
                    if response.status_code != 200:
                        raise RuntimeError(f"serving_sim rejected model request with HTTP {response.status_code}; no automatic reexecution")
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise RuntimeError("serving_sim did not return SSE")
                    async for kind, _, data in sse_events(response):
                        if 'first_sse' not in diagnostic['times']:
                            stage('first_sse')
                        if data == "[DONE]":
                            done = True
                            stage('done')
                            break
                        chunk = json.loads(data)
                        if kind == 'clawbox':
                            if (mapping is not None or chunk.get('protocol') != 3
                                    or chunk.get('session_id') != sid or chunk.get('request_id') != rid
                                    or chunk.get('replay') != expected):
                                raise RuntimeError('simulator adapter identity metadata mismatch')
                            mapping = chunk.get('process_sessions')
                            if not isinstance(mapping, dict) or any(
                                not isinstance(key, str) or not isinstance(value, dict)
                                or any(not isinstance(value.get(k), str) for k in ('session_id', 'pid', 'recorded_pid'))
                                for key, value in mapping.items()):
                                raise RuntimeError('simulator adapter process mapping is invalid')
                            continue
                        if chunk.get("error") is not None:
                            raise RuntimeError("serving_sim returned an error event")
                        if chunk.get("id") != rid:
                            raise RuntimeError("simulator response request identity mismatch")
                        if "replay" in chunk:
                            replay = chunk["replay"]
                            if (chunk.get("session_id") != sid or chunk.get("request_id") != rid
                                    or any(replay.get(k) != v for k, v in expected.items())):
                                raise RuntimeError("simulator response replay binding mismatch")
                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or len(choices) != 1:
                            raise RuntimeError("simulator response requires one choice")
                        reason = choices[0].get("finish_reason")
                        if reason is not None:
                            if "replay" not in chunk:
                                raise RuntimeError("simulator terminal output lacks replay identity")
                            if reason not in {"stop", "tool_calls", "length", "content_filter", "function_call"}:
                                raise RuntimeError("simulator response has an invalid finish reason")
                            finished = True
                            stage('terminal')
                        elif first is None:
                            if "replay" not in chunk:
                                raise RuntimeError("simulator first output lacks replay identity")
                            first = dict(received_unix_s=time.time(), virtual_s=chunk.get("t"))
                        message.append(choices[0].get('delta') or {})
                    if not done or not finished:
                        raise RuntimeError("simulator output ended without a successful terminal response and DONE")
                    if mapping is None:
                        raise RuntimeError('simulator output lacks ClawBox identity metadata')
            self.check()
            self.signal_log.write(json.dumps(dict(event="chat_complete", session_id=sid, request_id=rid,
                cluster_id=cluster_id,
                first_output=first, received_unix_s=time.time(), replay=expected,
                finish_virtual_s=chunk.get("t"), usage=chunk.get("usage"),
                finish_reason=chunk["choices"][0]["finish_reason"])) + "\n")
            # Guests already wait for the completed response in the host gateway.
            # Compact only the delivered representation; GPU pacing and signals
            # continue to be consumed at their original times.
            chunk['choices'][0]['delta'] = rebind_process_sessions(message.value(), mapping)
            self.diagnostics.write('request_completed', **diagnostic)
            return 200, "text/event-stream", ('data: ' + json.dumps(chunk, ensure_ascii=False)
                + '\n\ndata: [DONE]\n\n').encode()
        except BaseException as exc:
            diagnostic['error'] = exception_record(exc)
            diagnostic['cancelled'] = isinstance(exc, asyncio.CancelledError)
            if diagnostic['cancelled']:
                diagnostic['cancellation_reason'] = ('observation_failure' if self.error else
                    'session_cleanup' if rid in self.cancelled_requests else
                    'client_close' if self.stopping else 'caller_cancelled')
            self.diagnostics.write('request_failed', **diagnostic)
            exc.serving_sim_diagnostic = diagnostic
            if isinstance(exc, TimeoutError):
                error = TimeoutError('serving_sim model request timed out; GPU stream cancelled')
                error.serving_sim_diagnostic = diagnostic
                raise error from exc
            raise
        finally:
            self.callbacks.pop(rid, None)
            self.requests.pop(rid, None)

    def cancel(self, request_id: str):
        def cancel():
            self.cancelled_requests.add(request_id)
            task = self.requests.get(request_id)
            if task:
                task.cancel()
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(cancel)

    def validate_tool_outcomes(self, *, session_id, trace_id, spans, reported_failures):
        self.check()
        cluster_id = self.trace_clusters.get(trace_id)
        if cluster_id is None:
            raise RuntimeError(f"serving_sim trace has no cluster assignment: {trace_id}")
        failed = [s for s in spans if s.get('session_id') == session_id
                  and (s.get('status', {}).get('code') != 'ok'
                       or (s.get('output') or {}).get('exit_code') not in (None, 0))]
        async def validate():
            response = await self.client.post(
                self.bases[cluster_id] + '/v1/clawbox/simulator/tool-outcomes',
                json=dict(session_id=session_id, trace_id=trace_id, spans=failed,
                          reported_failures=reported_failures), timeout=60)
            response.raise_for_status()
            result = response.json()
            if result.get('session_id') != session_id or result.get('trace_id') != trace_id:
                raise RuntimeError('simulator tool validation identity mismatch')
            return result
        future = asyncio.run_coroutine_threadsafe(validate(), self.loop)
        try:
            return future.result(61)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError('simulator Tool outcome audit timed out') from None

    async def _close(self):
        self.stopping = True
        with self.feedback_lock:
            self.feedback_pending = None
        tasks = list(self.requests.values())
        if hasattr(self, "signal_tasks"):
            tasks.extend(self.signal_tasks)
        elif hasattr(self, "signal_task"):
            tasks.append(self.signal_task)
        if hasattr(self, 'sample_task'):
            tasks.append(self.sample_task)
        tasks.extend(self.feedback_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self, "client"):
            await self.client.aclose()
        await self.loop.shutdown_asyncgens()
        if hasattr(self, "signal_log"):
            self.signal_log.close()
        if hasattr(self, 'diagnostics'):
            self.diagnostics.close()

    def __exit__(self, *args):
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(20)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        self.loop.close()
