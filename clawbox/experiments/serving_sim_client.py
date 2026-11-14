"""Host-owned model streams and one shared GPU observation stream per experiment arm."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from clawbox.replay.process_sessions import rebind_process_sessions


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
        self.base = str(configuration["base_url"]).rstrip("/")
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.credential, self.output = credential, output
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="serving-sim", daemon=True)
        self.callbacks = {}
        self.requests = {}
        self.cancelled_requests = set()
        self.error = None
        self.stopping = False

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
        self.client = httpx.AsyncClient(trust_env=False,
            headers={"Authorization": "Bearer " + self.credential} if self.credential else {},
            timeout=httpx.Timeout(20, read=None),
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=32))
        sent = time.time()
        response = await self.client.get(self.base + "/v1/clawbox/simulator/info", timeout=20)
        received = time.time()
        response.raise_for_status()
        self.info = response.json()
        if self.info.get('adapter_protocol') != 2:
            raise ValueError('serving_sim requires ClawBox adapter protocol 2; restart with the current ClawBox launcher')
        self.clock_offset_s = ((sent + received) / 2 - float(self.info["server_wall_time"]))
        self.clock_uncertainty_s = max(0., received - sent) / 2
        self.info["client_clock_alignment"] = dict(offset_s=self.clock_offset_s,
            uncertainty_s=self.clock_uncertainty_s, method="HTTP request midpoint")
        if not self.info.get("ready") or self.info.get("clock", {}).get("k") != 1:
            raise ValueError("serving_sim requires a ready service with k=1 for real CPU execution")
        if type(self.info.get("accepted_requests")) is not int or self.info["accepted_requests"] != 0:
            raise ValueError("simulator already contains requests; restart it with a new output directory for this arm")
        expected_topology = self.configuration.get("serving_sim_topology_digest")
        if expected_topology is not None and self.info.get("topology_digest") != expected_topology:
            raise ValueError("simulator topology differs from inference.configuration.serving_sim_topology_digest")
        from .serving_sim_inputs import load_serving_sim_manifest
        manifest = load_serving_sim_manifest(Path(self.configuration["serving_sim_catalog_manifest"]), verify=True)
        if self.info.get("catalog_digest") != manifest["catalog"]["catalog_digest"]:
            raise ValueError("simulator catalog differs from serving_sim_catalog_manifest; start the matching catalog")
        self.case_bindings = {row['case_id']: row for row in manifest['cases']}
        for row in self.case_bindings.values():
            actual = self.info.get('traces', {}).get(row['trace_id'], {})
            if any(actual.get(k) != row[k] for k in ('model_calls', 'missing_reasoning_calls')):
                raise ValueError(f"{row['case_id']}: simulator trace counts differ from the CPU manifest; prepare a new bundle")
        self.epoch = self.info["service_id"]
        (self.output / "service.json").write_text(json.dumps(self.info, indent=2), encoding="utf-8")
        self.signal_log = (self.output / "observations.jsonl").open("a", encoding="utf-8")
        self.signal_ready = asyncio.Event()
        self.signal_task = asyncio.create_task(self._signals())
        await asyncio.wait_for(self.signal_ready.wait(), 20)
        self.check()

    async def _signals(self):
        try:
            cursor = self.info["signals"]["head"]
            async with self.client.stream("GET", self.base + "/request-signals/clawbox/events",
                    params={"after": cursor}) as response:
                response.raise_for_status()
                self.signal_ready.set()
                async for kind, identity, data in sse_events(response):
                    if kind == "gap":
                        raise RuntimeError("simulator signal history has a gap; the signal-driven arm cannot continue")
                    record = json.loads(data)
                    if not identity.startswith(self.epoch + ":"):
                        raise RuntimeError("simulator signal epoch changed")
                    received = time.time()
                    record.update(signal_sequence=int(identity.rsplit(":", 1)[1]),
                                  received_unix_s=received,
                                  signal_age_seconds=max(0., received - float(record["wall_t"])
                                      - self.clock_offset_s + self.clock_uncertainty_s))
                    self.signal_log.write(json.dumps(record, separators=(",", ":")) + "\n")
                    binding = self.callbacks.get(record.get("request_id"))
                    if binding:
                        session_id, callback = binding
                        if record.get("session_id") != session_id:
                            raise RuntimeError("simulator signal session identity mismatch")
                        if callback is not None:
                            callback(record)
                if not self.stopping:
                    raise RuntimeError("simulator signal stream disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            for task in list(self.requests.values()):
                task.cancel()
        finally:
            self.signal_ready.set()

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"serving_sim observation failed: {self.error}") from self.error

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
        forwarded = {**payload, "stream": True, "session_id": sid, "request_id": rid,
                     "replay": expected, "model": self.configuration["model"]}
        message, finished, done, first = _Message(), False, False, None
        mapping = None
        try:
            async with asyncio.timeout(timeout_s):
                async with self.client.stream("POST", self.base + "/v1/chat/completions", json=forwarded) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"serving_sim rejected model request with HTTP {response.status_code}; no automatic reexecution")
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise RuntimeError("serving_sim did not return SSE")
                    async for kind, _, data in sse_events(response):
                        if data == "[DONE]":
                            done = True
                            break
                        chunk = json.loads(data)
                        if kind == 'clawbox':
                            if (mapping is not None or chunk.get('protocol') != 2
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
                first_output=first, received_unix_s=time.time(), replay=expected,
                finish_virtual_s=chunk.get("t"), usage=chunk.get("usage"),
                finish_reason=chunk["choices"][0]["finish_reason"])) + "\n")
            # Guests already wait for the completed response in the host gateway.
            # Compact only the delivered representation; GPU pacing and signals
            # continue to be consumed at their original times.
            chunk['choices'][0]['delta'] = rebind_process_sessions(message.value(), mapping)
            return 200, "text/event-stream", ('data: ' + json.dumps(chunk, ensure_ascii=False)
                + '\n\ndata: [DONE]\n\n').encode()
        except TimeoutError:
            raise TimeoutError('serving_sim model request timed out; GPU stream cancelled') from None
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
        failed = [s for s in spans if s.get('session_id') == session_id
                  and (s.get('status', {}).get('code') != 'ok'
                       or (s.get('output') or {}).get('exit_code') not in (None, 0))]
        async def validate():
            response = await self.client.post(self.base + '/v1/clawbox/simulator/tool-outcomes',
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
        tasks = list(self.requests.values())
        if hasattr(self, "signal_task"):
            tasks.append(self.signal_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self, "client"):
            await self.client.aclose()
        await self.loop.shutdown_asyncgens()
        if hasattr(self, "signal_log"):
            self.signal_log.close()

    def __exit__(self, *args):
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(20)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        self.loop.close()
