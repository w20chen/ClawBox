"""Own one native simulator process for one supervised experiment arm."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
import psutil

from .spec import ServingSimLaunchSpec
from .serving_sim_diagnostics import exception_record


class ServingSimProcess:
    def __init__(self, configuration: dict, artifact_root: Path):
        self.configuration = configuration
        self.spec = ServingSimLaunchSpec.model_validate(configuration['serving_sim_launch'])
        self.root = artifact_root / 'gpu-service'
        self.out = self.root / 'output'
        self.process = None
        self.log = None
        self.receipt = dict(started=False, stopped=False, finalized=False,
                            shutdown_timeout_seconds=self.spec.shutdown_timeout_seconds)
        self.closed = False

    def persist(self):
        path = self.root / 'process.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.receipt, indent=2), encoding='utf-8')
        temporary.replace(path)

    def start(self):
        self.root.mkdir(parents=True, exist_ok=False)
        try:
            if sys.platform != 'linux':
                raise RuntimeError('managed serving_sim requires Linux; run on the configured experiment host')
            if not set(self.spec.cpu_affinity) <= os.sched_getaffinity(0):
                raise ValueError('simulator CPU affinity is outside the supervisor allowed CPUs')
            for name in ('sim_root', 'venv', 'trace_catalog', 'tokenizer_dir', 'cache_dir', 'hf_hub_cache'):
                value = getattr(self.spec, name)
                if value is not None and not Path(value).exists():
                    raise FileNotFoundError(f'serving_sim_launch.{name} does not exist: {value}')
            url = urlsplit(self.configuration['base_url'])
            port = url.port or 80
            # Refuse to adopt an unrelated listener. The returned PID is checked too.
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
            command = ['taskset', '-c', ','.join(map(str, self.spec.cpu_affinity)),
                sys.executable, '-m', 'clawbox.integrations.serving_sim', 'serve',
                '--sim-root', self.spec.sim_root, '--venv', self.spec.venv,
                '--cores', str(self.spec.cores), '--topology', self.spec.topology,
                '--trace-catalog', self.spec.trace_catalog, '--out', str(self.out),
                '--persistence', self.spec.persistence, '--nofile', str(self.spec.nofile),
                '--diagnostic-interval-seconds', str(self.configuration.get('serving_sim_diagnostic_interval_seconds', 1.)),
                '--host', '127.0.0.1', '--port', str(port), '--k', '1',
                '--api-key-env', self.configuration.get('api_key_env', 'OPENCLAW_API_KEY')]
            if self.spec.tokenizer_dir:
                command += ['--tokenizer-dir', self.spec.tokenizer_dir]
            self.log = (self.root / 'service.log').open('x', encoding='utf-8')
            environment = dict(os.environ)
            if self.spec.cache_dir:
                environment['SERVING_SIM_CACHE'] = self.spec.cache_dir
            if self.spec.hf_hub_cache:
                environment['HF_HUB_CACHE'] = self.spec.hf_hub_cache
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT,
                                            start_new_session=True, env=environment)
            self.identity = dict(pid=self.process.pid, create_time=psutil.Process(self.process.pid).create_time())
            self.receipt.update(started=True, identity=self.identity, command=command, started_unix_s=time.time())
            self.persist()
            from .llm_config import resolve_llm_configuration
            _, credential = resolve_llm_configuration(self.configuration, live=False)
            headers = {'Authorization': 'Bearer ' + credential} if credential else {}
            deadline = time.monotonic() + self.spec.startup_timeout_seconds
            with httpx.Client(trust_env=False, timeout=1., headers=headers) as client:
                while time.monotonic() < deadline:
                    self.check()
                    try:
                        response = client.get(f'http://127.0.0.1:{port}/v1/clawbox/simulator/info')
                        response.raise_for_status()
                        info = response.json()
                    except (httpx.HTTPError, ValueError):
                        time.sleep(.2)
                        continue
                    if info.get('pid') != self.process.pid:
                        raise RuntimeError('simulator endpoint is not owned by this launch')
                    if info.get('ready') and info.get('accepted_requests') == 0:
                        if info.get('clock', {}).get('k') != 1 or info.get('cpu_affinity') != sorted(self.spec.cpu_affinity):
                            raise RuntimeError('simulator clock or CPU affinity differs from requested configuration')
                        self.receipt['service'] = info
                        self.persist()
                        return self
                    time.sleep(.2)
            raise TimeoutError(f'simulator startup exceeded {self.spec.startup_timeout_seconds}s; inspect {self.root / "service.log"}')
        except BaseException as exc:
            self.receipt['startup_error'] = exception_record(exc)
            self.stop()
            raise

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(f'simulator exited unexpectedly with status {self.process.returncode}; inspect {self.root / "service.log"}')

    def stop(self) -> dict:
        if self.closed:
            return self.receipt
        self.closed = True
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    owner = psutil.Process(self.process.pid)
                    if abs(owner.create_time() - self.identity['create_time']) >= .01:
                        raise RuntimeError('simulator PID identity changed; refusing to signal another process')
                    self.receipt['shutdown_signal'] = int(signal.SIGTERM)
                    owner.terminate()
                    deadline = time.monotonic() + self.spec.shutdown_timeout_seconds
                    while self.process.poll() is None and time.monotonic() < deadline:
                        self.receipt['shutdown_heartbeat_unix_s'] = time.time()
                        self.persist()
                        time.sleep(.5)
                    if self.process.poll() is None:
                        from .supervisor import terminate_process_tree
                        terminate_process_tree(self.process.pid)
                        self.process.wait(timeout=10)
                        raise TimeoutError('simulator graceful shutdown timed out; reports are incomplete')
                self.receipt.update(stopped=True, exit_code=self.process.returncode)
                marker = json.loads((self.out / 'clawbox-persistence.json').read_text())
                self.receipt['persistence'] = marker
                if not marker.get('finalized') or not marker.get('native_close_succeeded'):
                    raise RuntimeError('simulator did not finalize after successful native close')
                validity = json.loads((self.out / 'validity.json').read_text())
                if not (self.out / 'realized.json').is_file():
                    raise RuntimeError('simulator final realized.json is missing')
                self.receipt['validity'] = {k: validity.get(k) for k in ('lag', 'warning_threshold_exceeded')}
                self.receipt['finalized'] = True
                # Uvicorn re-raises its caught signal after a graceful lifespan
                # close. Preserve that exit code, but do not call a verified
                # requested shutdown a service failure.
                allowed_exit = {0}
                if self.receipt.get('shutdown_signal') == int(signal.SIGTERM):
                    allowed_exit.add(-int(signal.SIGTERM))
                if self.process.returncode not in allowed_exit:
                    raise RuntimeError(f'simulator exited with status {self.process.returncode}')
        except Exception as exc:
            self.receipt['shutdown_error'] = exception_record(exc)
        finally:
            if self.process is not None:
                self.receipt['stopped'] = self.process.poll() is not None
                self.receipt['exit_code'] = self.process.returncode
            if self.log is not None:
                self.log.close()
            self.receipt['closed_unix_s'] = time.time()
            self.persist()
        return self.receipt


def finalize_saved_service(root: Path) -> dict:
    """Cleanup after a supervisor interruption; never adopt a URL or reused PID."""
    from .supervisor import process_is_alive, terminate_process_tree
    receipt = json.loads((root / 'process.json').read_text())
    identity = receipt.get('identity')
    if process_is_alive(identity):
        process = psutil.Process(identity['pid'])
        if str(root / 'output') not in process.cmdline():
            raise RuntimeError('saved GPU command identity differs; refusing to signal')
        process.terminate()
        deadline = time.monotonic() + float(receipt['shutdown_timeout_seconds'])
        while process_is_alive(identity) and time.monotonic() < deadline:
            time.sleep(.5)
        if process_is_alive(identity):
            terminate_process_tree(identity['pid'])
            raise RuntimeError('GPU cleanup needed forced termination; final reports are incomplete')
    marker_path = root / 'output' / 'clawbox-persistence.json'
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    receipt.update(stopped=True, persistence=marker, recovered_unix_s=time.time(),
                   finalized=bool(marker.get('finalized') and marker.get('native_close_succeeded')))
    temporary = root / 'process.tmp'
    temporary.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    temporary.replace(root / 'process.json')
    if not receipt['finalized']:
        raise RuntimeError('GPU process is gone but final reports are incomplete; preserve failed run and use a new run ID')
    return receipt
