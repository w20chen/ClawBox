"""Read-only launch checks with measured facts and actionable remedies."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import urllib.request


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or shlex.join(args))
    return result.stdout.strip()


def load_standard(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('creation standard must be a schema_version 1 manifest')
    if not isinstance(value.get('host_kernel'), str) or not isinstance(value.get('cube_api_nofile_minimum'), int):
        raise ValueError('creation standard requires host_kernel and cube_api_nofile_minimum')
    if value['cube_api_nofile_minimum'] < 1024:
        raise ValueError('creation standard cube_api_nofile_minimum must be >= 1024')
    boot = value.get('host_specific_boot_parameters', [])
    if not isinstance(boot, list) or not all(isinstance(x, str) for x in boot):
        raise ValueError('creation standard host_specific_boot_parameters must be a list')
    taps = value.get('required_unowned_ready_taps_full1280', 0)
    if not isinstance(taps, int) or taps < 0:
        raise ValueError('creation standard required_unowned_ready_taps_full1280 must be a nonnegative integer')
    return value


def tap_inventory(url: str) -> dict:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as response:
        value = json.load(response)
    taps = value['taps']
    if not isinstance(taps, list):
        raise ValueError('Cubelet returned a non-list taps inventory')
    ready = {t['tapName'] for t in taps if t.get('state') == 'Ready'
             and not any(t.get(k) for k in ('ownerSandboxID', 'sandboxID', 'networkHandle'))}
    return dict(total=len(taps), ready_unowned=len(ready))


def active_experiments() -> list[dict]:
    import psutil
    owners = {}
    own_processes = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    for process in psutil.process_iter(['pid', 'cmdline']):
        if process.pid in own_processes:
            continue
        args = process.info['cmdline'] or []
        if not any(x in args for x in ('clawbox.experiments.worker', 'clawbox.cli')):
            continue
        if '--run-id' not in args:
            continue
        try:
            rid = args[args.index('--run-id') + 1]
            output = args[args.index('--output-root') + 1] if '--output-root' in args else None
            owners.setdefault(rid, dict(run_id=rid, pid=process.pid, output_root=output))
        except IndexError:
            continue
    return list(owners.values())


def idle_domain(path: str) -> dict:
    root = Path(path)
    events = root / 'cgroup.events'
    if events.exists() and 'populated 1' in events.read_text():
        raise RuntimeError(f'VM domain is occupied: {path}; owners={active_experiments()}')
    lock = Path('/run/clawbox/cgroups') / (hashlib.sha256(str(root).encode()).hexdigest() + '.lock')
    if lock.exists():
        import fcntl
        with lock.open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f'cgroup lease is held; owners={active_experiments()}') from exc
    return dict(domain=str(root), idle=True)


def process_nofile(unit: str) -> dict:
    pid = int(command('systemctl', 'show', unit, '-p', 'MainPID', '--value'))
    if pid <= 0:
        raise RuntimeError(f'{unit} has no running MainPID')
    import psutil
    soft, hard = psutil.Process(pid).rlimit(psutil.RLIMIT_NOFILE)
    return dict(pid=pid, soft=soft, hard=hard)


def check_ports(configuration: dict) -> dict:
    from .spec import ServingSimLaunchSpec
    from .serving_sim_client import serving_sim_bases
    from urllib.parse import urlsplit
    launch = ServingSimLaunchSpec.model_validate(configuration['serving_sim_launch'])
    for key in ('sim_root', 'venv', 'trace_catalog', 'tokenizer_dir', 'cache_dir', 'hf_hub_cache'):
        value = getattr(launch, key)
        if value is not None and not Path(value).exists():
            raise FileNotFoundError(f'serving_sim_launch.{key}: {value}')
    if not os.access(Path(launch.venv) / 'bin/python', os.X_OK):
        raise ValueError(f'{launch.venv}/bin/python is not executable; run serving_sim setup with this venv')
    if not set(launch.cpu_affinity) <= os.sched_getaffinity(0):
        raise ValueError('simulator cpu_affinity is outside the current process allowed CPUs')
    low, high = map(int, Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())
    reserved = set()
    for part in Path('/proc/sys/net/ipv4/ip_local_reserved_ports').read_text().strip().split(','):
        if part:
            a, _, b = part.partition('-')
            reserved.update(range(int(a), int(b or a) + 1))
    ports = [urlsplit(url).port for url in serving_sim_bases(configuration)]
    unsafe = [p for p in ports if low <= p <= high and p not in reserved]
    if unsafe:
        raise ValueError(f'simulator ports {unsafe} overlap ephemeral range {low}-{high}; '
                         'choose base_url outside that range or explicitly reserve the ports in host networking')
    sockets = []
    try:
        for port in ports:
            probe = socket.socket()
            sockets.append(probe)
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RuntimeError(f'simulator port {port} is occupied; inspect ss -lntp, '
                                   'choose another base_url or stop only its owning service') from exc
    finally:
        for probe in sockets:
            probe.close()
    return dict(ports=ports, ephemeral_range=[low, high], cpu_affinity=launch.cpu_affinity)


def prerequisites(spec, *, output_root: Path | None = None) -> dict:
    """Cheap facts checked again by launch/run/qualify; never repair or start VMs."""
    from .qualification import host_profile_for
    checks = []
    def probe(name, operation, remedy):
        try:
            detail = operation()
            ok = True
        except Exception as exc:
            ok, detail = False, f'{type(exc).__name__}: {exc}'
        checks.append(dict(check=name, ok=ok, detail=detail, remedy=None if ok else remedy))
    def require(condition, detail):
        if not condition:
            raise RuntimeError(str(detail))
        return detail
    from .configuration import require_configured_node
    probe('configured target node', lambda: require_configured_node(spec.resources.target_node),
          'Unset CLAWBOX_WORKER_NODE; select environment.resources.target_node in baseline.yaml.')
    probe('Linux experiment host', lambda: require(platform.system() == 'Linux', platform.system()),
          'Run this command on the configured Linux experiment host.')
    if platform.system() != 'Linux':
        return dict(ready=False, checks=checks)
    host = None
    if path := os.getenv('CLAWBOX_HOST_CONFIG'):
        def read_host():
            nonlocal host
            from .host import load_config
            host = load_config(Path(path))
            return dict(path=path, note='readiness constraints only; baseline resources are not overridden')
        probe('selected host plan', read_host, 'Fix CLAWBOX_HOST_CONFIG or pass --host-config with the reviewed YAML.')
    standard = {}
    if path := os.getenv('CLAWBOX_CREATION_STANDARD'):
        def read_standard():
            nonlocal standard
            standard = load_standard(Path(path))
            return dict(path=path, reference_run_id=standard.get('reference_run_id'),
                        scope='kernel, boot parameters, TAP reserve and CubeAPI NOFILE; native build provenance remains separately qualified')
        probe('creation standard', read_standard, 'Select the saved creation manifest; do not substitute a qualification receipt.')
    for expected_kernel in dict.fromkeys(filter(None, [host.required_kernel if host else None, standard.get('host_kernel')])):
        probe('host kernel', lambda: require(platform.release() == expected_kernel,
              dict(actual=platform.release(), expected=expected_kernel)),
              'Boot the reviewed kernel; keep old kernels/templates. Do not silently change the experiment baseline.')
    boot = set((host.required_boot_parameters if host else []) + standard.get('host_specific_boot_parameters', []))
    if boot:
        probe('host boot parameters', lambda: require(boot <= set(Path('/proc/cmdline').read_text().split()),
              dict(required=sorted(boot), actual=Path('/proc/cmdline').read_text().strip())),
              'Restore the saved host boot settings and reboot during maintenance.')
    probe('exclusive VM domain', lambda: idle_domain(spec.resources.local_memory_cgroup),
          'Use experiment status RUN_ID in its owning output root; wait for an active run, or explicitly abort an owned stale run. Never delete lock files.')
    demand = host_profile_for(spec)['cube_capacity_demand']['vm_count']
    required_taps = max(demand, int(standard.get('required_unowned_ready_taps_full1280', 0)) if demand >= 2560 else 0)
    def taps():
        result = tap_inventory(host.cubelet_tap_url if host else 'http://127.0.0.1:9998/v1/network/taps')
        result['required'] = required_taps
        return require(result['ready_unowned'] >= required_taps, result)
    probe('Ready unowned TAPs', taps,
          f'Wait for Cubelet TAP preparation. If its target is too small, set host.yaml cubelet_tap_init_num >= {required_taps}, then host check/apply on an idle host.')
    nofile = max(65536 if demand >= 2560 else 1024,
                 (host.cube_api_nofile if host else None) or 0,
                 int(standard.get('cube_api_nofile_minimum', 0)))
    def api_limit():
        result = process_nofile('cube-sandbox-cube-api')
        return require(result['soft'] == -1 or result['soft'] >= nofile, dict(result, required=nofile))
    probe('CubeAPI actual NOFILE', api_limit,
          f'Set cube_api_nofile: {nofile} in host.yaml and apply on an idle host; a shell ulimit does not change CubeAPI.')
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    client_required = 65536 if demand >= 2560 else 1024
    probe('experiment process NOFILE', lambda: require(soft == -1 or soft >= client_required,
          dict(soft=soft, hard=hard, required=client_required)),
          f'Set CLAWBOX_NOFILE_LIMIT={client_required} in the selected machine.env; raise the login hard limit if necessary, then start a fresh wrapper invocation.')
    minimum = (host.minimum_disk_free_gib if host else 20) * 1024**3
    paths = [spec.resources.cubelet_storage_root, spec.resources.cold_snapshot_root,
             str(output_root) if output_root else None]
    for path in dict.fromkeys(p for p in paths if p):
        def disk(path=path):
            parent = Path(path)
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            free = shutil.disk_usage(parent).free
            return require(free >= minimum, dict(path=path, free_gib=free/1024**3, minimum_gib=minimum/1024**3))
        probe(f'disk reserve: {path}', disk,
              'Preview experiment host clean for old crash dumps, or provision capacity. Preserve VM/template storage and frozen results.')
    if 'serving_sim_launch' in spec.inference.configuration:
        probe('managed GPU simulator', lambda: check_ports(spec.inference.configuration),
              'Fix the explicit serving_sim_launch paths/CPU affinity or base_url in baseline.yaml; no guest rebuild is needed for a port change.')
    return dict(ready=all(c['ok'] for c in checks), required_vm_slots=demand, checks=checks)


def require_prerequisites(spec) -> None:
    report = prerequisites(spec)
    failed = [c for c in report['checks'] if not c['ok']]
    if failed:
        raise RuntimeError('launch preflight failed:\n' + '\n'.join(
            f"- {c['check']}: {c['detail']}\n  Remedy: {c['remedy']}" for c in failed))


def check(spec, *, output_root: Path, qualification: Path | None = None) -> dict:
    from .inputs import validate_launch_inputs
    from .qualification import require_host_ready, validate_receipt
    report = prerequisites(spec, output_root=output_root)
    for name, operation, remedy in [
        ('input files and readiness metadata', lambda: validate_launch_inputs(spec),
         'Fix the paths/identities in baseline.yaml and workload.yaml; use experiment validate --inputs after replacing input bytes.'),
        ('services, templates and resource configuration',
         lambda: require_host_ready(spec, launch_checks=False),
         'Review the reported FAIL lines. Use host recover after reboot, or host check/apply for a reviewed configuration change.'),
    ]:
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                operation()
            ok, detail = True, output.getvalue().splitlines()
        except Exception as exc:
            ok, detail = False, dict(error=str(exc), observations=output.getvalue().splitlines())
        report['checks'].append(dict(check=name, ok=ok, detail=detail, remedy=None if ok else remedy))
    if qualification:
        try:
            validate_receipt(spec, qualification)
            report['qualification'] = dict(path=str(qualification), reusable=True)
        except (OSError, ValueError) as exc:
            report['qualification'] = dict(path=str(qualification), reusable=False, reason=str(exc))
            report['checks'].append(dict(check='explicit qualification receipt', ok=False,
                detail=str(exc), remedy='Pass a matching receipt or omit --qualification so launch qualifies automatically.'))
    else:
        report['qualification'] = dict(reusable=None, action='launch checks its saved receipt and qualifies only when needed')
    report['ready'] = all(c['ok'] for c in report['checks'])
    report['scope'] = 'read-only readiness; does not establish Full1280 performance or replace real-VM qualification'
    return report
