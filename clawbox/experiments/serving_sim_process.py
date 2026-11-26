"""Own the independent native simulator clusters for one experiment arm."""
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


def final_reports(out: Path) -> dict:
    """Current native realized records reference these incremental sidecars."""
    for name in ('realized.json', 'realized-inputs.jsonl', 'realized-tokens.bin'):
        if not (out / name).is_file():
            raise RuntimeError(f'simulator final {name} is missing; preserve the incomplete run')
    validity = json.loads((out / 'validity.json').read_text())
    if validity.get('prompt_source') != 'actual':
        raise RuntimeError('simulator final report does not confirm actual Agent prompts')
    # Keep native timing caveats (including non_physical). Current sim-release
    # reports lag measurements without a warning_threshold_exceeded verdict.
    return validity


def serving_sim_cluster_layouts(
    configuration: dict, spec: ServingSimLaunchSpec, root: Path,
) -> list[dict]:
    """Plan ports, CPU binding and artifact paths without starting processes."""
    base_port = urlsplit(configuration['base_url']).port
    if base_port is None:
        raise ValueError('managed serving_sim requires an explicit base_url port')
    layouts = []
    for cluster_id in range(spec.cluster_count):
        cluster_root = root if spec.cluster_count == 1 else root / 'clusters' / f'{cluster_id:03d}'
        cpus = (list(spec.cpu_affinity) if spec.cluster_count == 1
                else [spec.cpu_affinity[cluster_id % len(spec.cpu_affinity)]])
        layouts.append(dict(
            cluster_id=cluster_id,
            port=base_port + cluster_id,
            cpu_affinity=cpus,
            cores=spec.cores if spec.cluster_count == 1 else 1,
            root=cluster_root,
            output=cluster_root / 'output',
            log=cluster_root / 'service.log',
        ))
    return layouts


class ServingSimProcess:
    def __init__(self, configuration: dict, artifact_root: Path):
        self.configuration = configuration
        self.spec = ServingSimLaunchSpec.model_validate(configuration['serving_sim_launch'])
        self.root = artifact_root / 'gpu-service'
        self.layouts = serving_sim_cluster_layouts(configuration, self.spec, self.root)

        # Keep the historical single-service attributes for callers and receipts.
        self.out = self.layouts[0]['output']
        self.process = None
        self.identity = None
        self.log = None
        self.processes = []
        self.identities = []
        self.logs = []
        self.receipt = dict(
            started=False,
            stopped=False,
            finalized=False,
            cluster_count=self.spec.cluster_count,
            routing=self.spec.routing,
            shutdown_timeout_seconds=self.spec.shutdown_timeout_seconds,
            clusters=[self._planned_receipt(layout) for layout in self.layouts],
        )
        self.closed = False

    @staticmethod
    def _planned_receipt(layout: dict) -> dict:
        return dict(
            cluster_id=layout['cluster_id'],
            port=layout['port'],
            cpu_affinity=layout['cpu_affinity'],
            cores=layout['cores'],
            root=str(layout['root']),
            output=str(layout['output']),
            log=str(layout['log']),
            started=False,
            stopped=False,
            finalized=False,
        )

    def persist(self):
        path = self.root / 'process.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.receipt, indent=2), encoding='utf-8')
        temporary.replace(path)

    def _command(self, layout: dict) -> list[str]:
        command = [
            'taskset', '-c', ','.join(map(str, layout['cpu_affinity'])),
            sys.executable, '-m', 'clawbox.integrations.serving_sim', 'serve',
            '--sim-root', self.spec.sim_root, '--venv', self.spec.venv,
            '--cores', str(layout['cores']), '--topology', self.spec.topology,
            '--trace-catalog', self.spec.trace_catalog, '--out', str(layout['output']),
            '--persistence', self.spec.persistence, '--nofile', str(self.spec.nofile),
            '--diagnostic-interval-seconds',
            str(self.configuration.get('serving_sim_diagnostic_interval_seconds', 1.)),
            '--host', '127.0.0.1', '--port', str(layout['port']), '--k', '1',
            '--api-key-env', self.configuration.get('api_key_env', 'OPENCLAW_API_KEY'),
        ]
        if self.spec.tokenizer_dir:
            command += ['--tokenizer-dir', self.spec.tokenizer_dir]
        for name in ('decode_time_scale', 'tpot_ms'):
            value = getattr(self.spec, name)
            if value is not None:
                command += ['--' + name.replace('_', '-'), str(value)]
        for name in (
            'forecast_interval_seconds', 'forecast_window_seconds', 'forecast_validity_seconds',
            'cpu_refill_mode', 'cpu_refill_feedback_interval_seconds',
            'cpu_refill_activation_floor_utilization',
            'cpu_refill_enter_utilization', 'cpu_refill_exit_utilization',
            'cpu_refill_enter_samples', 'cpu_refill_stale_seconds',
            'cpu_refill_throttle_steps', 'cpu_refill_release_steps',
        ):
            command += ['--' + name.replace('_', '-'), str(getattr(self.spec, name))]
        return command

    def _entries(self) -> list[tuple[int, dict, object, dict | None, object | None]]:
        if self.processes:
            return [
                (index, self.layouts[index], process, self.identities[index], self.logs[index])
                for index, process in enumerate(self.processes)
            ]
        if self.process is not None:
            return [(0, self.layouts[0], self.process, self.identity, self.log)]
        return []

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

            # Hold every port during the complete preflight so a partially valid
            # range never starts a subset of the pool.
            probes = []
            try:
                for layout in self.layouts:
                    probe = socket.socket()
                    probes.append(probe)
                    probe.bind(('127.0.0.1', layout['port']))
            finally:
                for probe in probes:
                    probe.close()

            environment = dict(os.environ)
            if self.spec.cache_dir:
                environment['SERVING_SIM_CACHE'] = self.spec.cache_dir
            if self.spec.hf_hub_cache:
                environment['HF_HUB_CACHE'] = self.spec.hf_hub_cache

            # Start the whole pool before waiting for readiness. Initialization
            # and model/catalog loading therefore overlap across clusters.
            for layout in self.layouts:
                layout['root'].mkdir(parents=True, exist_ok=self.spec.cluster_count == 1)
                command = self._command(layout)
                log = layout['log'].open('x', encoding='utf-8')
                try:
                    process = subprocess.Popen(
                        command, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, env=environment,
                    )
                except BaseException:
                    log.close()
                    raise
                identity = dict(
                    pid=process.pid,
                    create_time=psutil.Process(process.pid).create_time(),
                )
                self.processes.append(process)
                self.identities.append(identity)
                self.logs.append(log)
                row = self.receipt['clusters'][layout['cluster_id']]
                row.update(
                    started=True, identity=identity, command=command,
                    started_unix_s=time.time(),
                )
                self.persist()

            self.process = self.processes[0]
            self.identity = self.identities[0]
            self.log = self.logs[0]
            self.receipt.update(
                started=True,
                identity=self.identity,
                command=self.receipt['clusters'][0]['command'],
                started_unix_s=min(row['started_unix_s'] for row in self.receipt['clusters']),
            )
            self.persist()

            from .llm_config import resolve_llm_configuration
            _, credential = resolve_llm_configuration(self.configuration, live=False)
            headers = {'Authorization': 'Bearer ' + credential} if credential else {}
            deadline = time.monotonic() + self.spec.startup_timeout_seconds
            pending = set(range(len(self.layouts)))
            with httpx.Client(trust_env=False, timeout=1., headers=headers) as client:
                while pending and time.monotonic() < deadline:
                    self.check()
                    for index in list(pending):
                        layout = self.layouts[index]
                        try:
                            response = client.get(
                                f"http://127.0.0.1:{layout['port']}/v1/clawbox/simulator/info"
                            )
                            response.raise_for_status()
                            info = response.json()
                        except (httpx.HTTPError, ValueError):
                            continue
                        if info.get('pid') != self.processes[index].pid:
                            raise RuntimeError(
                                f"simulator cluster {index} endpoint is not owned by this launch"
                            )
                        if info.get('ready') and info.get('accepted_requests') == 0:
                            expected_decode = {name: getattr(self.spec, name)
                                for name in ('decode_time_scale', 'tpot_ms')
                                if getattr(self.spec, name) is not None}
                            if info.get('decode_time') != expected_decode:
                                raise RuntimeError(f'simulator cluster {index} decode timing differs '
                                                   'from requested configuration')
                            actual_policy = (info.get('cpu_refill') or {}).get('config')
                            if actual_policy != self.spec.cpu_refill_configuration():
                                raise RuntimeError(
                                    f'simulator cluster {index} cpu_refill policy differs '
                                    'from requested configuration'
                                )
                            if (info.get('clock', {}).get('k') != 1
                                    or info.get('cpu_affinity') != sorted(layout['cpu_affinity'])):
                                raise RuntimeError(
                                    f"simulator cluster {index} clock or CPU affinity differs "
                                    "from requested configuration"
                                )
                            self.receipt['clusters'][index]['service'] = info
                            pending.remove(index)
                            self.persist()
                    if pending:
                        time.sleep(.2)
            if pending:
                missing = ','.join(map(str, sorted(pending)))
                raise TimeoutError(
                    f"simulator clusters {missing} startup exceeded "
                    f"{self.spec.startup_timeout_seconds}s; inspect {self.root}"
                )
            self.receipt['services'] = [row['service'] for row in self.receipt['clusters']]
            self.receipt['service'] = self.receipt['services'][0]
            self.receipt['ready_unix_s'] = time.time()
            self.persist()
            return self
        except BaseException as exc:
            self.receipt['startup_error'] = exception_record(exc)
            self.stop()
            raise

    def check(self):
        for index, _, process, _, _ in self._entries():
            if process.poll() is not None:
                raise RuntimeError(
                    f"simulator cluster {index} exited unexpectedly with status "
                    f"{process.returncode}; inspect {self.layouts[index]['log']}"
                )

    def _record_single_compatibility(self):
        if self.spec.cluster_count != 1:
            return
        row = self.receipt['clusters'][0]
        for key in ('stopped', 'exit_code', 'persistence', 'validity', 'finalized'):
            if key in row:
                self.receipt[key] = row[key]

    def stop(self) -> dict:
        if self.closed:
            return self.receipt
        self.closed = True
        entries = self._entries()
        errors = []
        try:
            owners = {}
            # Verify every live PID before signaling any member of the pool.
            for index, _, process, identity, _ in entries:
                if process.poll() is None:
                    owner = psutil.Process(process.pid)
                    if identity is None or abs(owner.create_time() - identity['create_time']) >= .01:
                        raise RuntimeError(
                            f'simulator cluster {index} PID identity changed; refusing to signal another process'
                        )
                    owners[index] = owner
            if owners:
                self.receipt['shutdown_signal'] = int(signal.SIGTERM)
                for index, owner in owners.items():
                    self.receipt['clusters'][index]['shutdown_signal'] = int(signal.SIGTERM)
                    owner.terminate()
                deadline = time.monotonic() + self.spec.shutdown_timeout_seconds
                while (any(process.poll() is None for _, _, process, _, _ in entries)
                       and time.monotonic() < deadline):
                    self.receipt['shutdown_heartbeat_unix_s'] = time.time()
                    self.persist()
                    time.sleep(.5)
                for index, _, process, _, _ in entries:
                    if process.poll() is None:
                        from .supervisor import terminate_process_tree
                        terminate_process_tree(process.pid)
                        process.wait(timeout=10)
                        errors.append(
                            f'cluster {index}: graceful shutdown timed out; reports are incomplete'
                        )

            for index, layout, process, _, _ in entries:
                row = self.receipt['clusters'][index]
                row.update(stopped=process.poll() is not None, exit_code=process.returncode)
                try:
                    marker = json.loads(
                        (layout['output'] / 'clawbox-persistence.json').read_text()
                    )
                    row['persistence'] = marker
                    if not marker.get('finalized') or not marker.get('native_close_succeeded'):
                        raise RuntimeError(
                            'simulator did not finalize after successful native close'
                        )
                    row['validity'] = final_reports(layout['output'])
                    allowed_exit = {0}
                    if (row.get('shutdown_signal') == int(signal.SIGTERM)
                            or self.receipt.get('shutdown_signal') == int(signal.SIGTERM)):
                        allowed_exit.add(-int(signal.SIGTERM))
                    if process.returncode not in allowed_exit:
                        raise RuntimeError(f'simulator exited with status {process.returncode}')
                    row['finalized'] = True
                except Exception as exc:
                    row['finalized'] = False
                    row['shutdown_error'] = exception_record(exc)
                    errors.append(f'cluster {index}: {type(exc).__name__}: {exc}')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
        finally:
            for index, _, process, _, _ in entries:
                row = self.receipt['clusters'][index]
                row['stopped'] = process.poll() is not None
                row['exit_code'] = process.returncode
            for log in dict.fromkeys([entry[4] for entry in entries if entry[4] is not None]):
                log.close()
            self.receipt['stopped'] = bool(entries) and all(
                row.get('stopped') for row in self.receipt['clusters'][:len(entries)]
            )
            self.receipt['finalized'] = bool(entries) and len(entries) == len(self.layouts) and all(
                row.get('finalized') for row in self.receipt['clusters']
            )
            if self.spec.cluster_count > 1 and self.receipt['finalized']:
                validities = [row['validity'] for row in self.receipt['clusters']]
                self.receipt['validity'] = {
                    'cluster_count': self.spec.cluster_count,
                    'prompt_source': ('actual' if all(
                        item.get('prompt_source') == 'actual' for item in validities
                    ) else None),
                    'clusters': [
                        {'cluster_id': index, **item}
                        for index, item in enumerate(validities)
                    ],
                }
            if errors:
                self.receipt['shutdown_error'] = exception_record(RuntimeError('; '.join(errors)))
            self._record_single_compatibility()
            self.receipt['closed_unix_s'] = time.time()
            self.persist()
        return self.receipt


def _saved_cluster_rows(root: Path, receipt: dict) -> list[tuple[dict, Path]]:
    rows = receipt.get('clusters')
    if not rows:
        return [(receipt, root / 'output')]
    count = int(receipt.get('cluster_count', len(rows)))
    return [
        (row, (root / 'output' if count == 1
               else root / 'clusters' / f"{int(row['cluster_id']):03d}" / 'output'))
        for row in rows
    ]


def finalize_saved_service(root: Path) -> dict:
    """Cleanup after a supervisor interruption; never adopt a URL or reused PID."""
    from .supervisor import process_is_alive, terminate_process_tree
    receipt = json.loads((root / 'process.json').read_text())
    saved = _saved_cluster_rows(root, receipt)
    live = []
    for row, out in saved:
        identity = row.get('identity')
        if process_is_alive(identity):
            process = psutil.Process(identity['pid'])
            if str(out) not in process.cmdline():
                raise RuntimeError('saved GPU command identity differs; refusing to signal')
            live.append((row, process, identity))
    for row, process, _ in live:
        row['shutdown_signal'] = int(signal.SIGTERM)
        process.terminate()
    deadline = time.monotonic() + float(receipt['shutdown_timeout_seconds'])
    while any(process_is_alive(identity) for _, _, identity in live) and time.monotonic() < deadline:
        time.sleep(.5)
    forced = []
    for row, _, identity in live:
        if process_is_alive(identity):
            terminate_process_tree(identity['pid'])
            forced.append(str(row.get('cluster_id', 0)))

    errors = []
    for row, out in saved:
        marker_path = out / 'clawbox-persistence.json'
        marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
        row['persistence'] = marker
        row['stopped'] = not process_is_alive(row.get('identity'))
        row['recovered_unix_s'] = time.time()
        finalized = bool(marker.get('finalized') and marker.get('native_close_succeeded'))
        if finalized:
            try:
                row['validity'] = final_reports(out)
            except Exception as exc:
                finalized = False
                row['shutdown_error'] = exception_record(exc)
        row['finalized'] = finalized
        if not finalized:
            errors.append(f"cluster {row.get('cluster_id', 0)} final reports are incomplete")
    if forced:
        errors.append('forced termination for clusters ' + ','.join(forced))

    receipt['stopped'] = all(row.get('stopped') for row, _ in saved)
    receipt['finalized'] = all(row.get('finalized') for row, _ in saved)
    receipt['recovered_unix_s'] = time.time()
    if len(saved) == 1:
        row, _ = saved[0]
        for key in ('persistence', 'validity'):
            if key in row:
                receipt[key] = row[key]
    elif receipt['finalized']:
        validities = [row['validity'] for row, _ in saved]
        receipt['validity'] = {
            'cluster_count': len(saved),
            'prompt_source': ('actual' if all(
                item.get('prompt_source') == 'actual' for item in validities
            ) else None),
            'clusters': [
                {'cluster_id': row.get('cluster_id', index), **row['validity']}
                for index, (row, _) in enumerate(saved)
            ],
        }
    if errors:
        receipt['shutdown_error'] = exception_record(RuntimeError('; '.join(errors)))
    temporary = root / 'process.tmp'
    temporary.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    temporary.replace(root / 'process.json')
    if not receipt['finalized'] or forced:
        raise RuntimeError(
            'GPU process is gone but final reports are incomplete; preserve failed run and use a new run ID'
        )
    return receipt
