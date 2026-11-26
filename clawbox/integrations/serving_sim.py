"""Set up and launch the external GPU simulator without editing its source."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def require_host():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise ValueError('use Linux x86-64 or aarch64 for the simulator')
    if platform.machine() == 'aarch64':
        name, version = platform.libc_ver()
        if name != 'glibc' or tuple(map(int, version.split('.')[:2])) < (2, 38):
            raise ValueError('the ClawBox ARM dependency lock requires glibc >= 2.38')


def metadata():
    from serving_sim.setup import MODELS, METADATA
    from serving_sim.joined.joined import local_tokenizer_directory
    from huggingface_hub import snapshot_download
    for model, revision in MODELS.items():
        try:
            directory = local_tokenizer_directory(model, revision)
        except FileNotFoundError:
            directory = snapshot_download(model, revision=revision, allow_patterns=METADATA, max_workers=1)
            directory = local_tokenizer_directory(model, revision, Path(directory))
        print(model, revision, directory, flush=True)


def setup(args, python: Path):
    from serving_sim.setup import MOONCAKE_COMMIT
    missing = [name for name in ('uv', 'git', 'cargo', 'cc', 'docker') if shutil.which(name) is None]
    if missing:
        raise ValueError('install prerequisites first: ' + ', '.join(missing))
    def run(command, **kwargs):
        subprocess.run(list(map(str, command)), check=True, **kwargs)
    run(['docker', 'info', '--format', '{{.ServerVersion}}'], stdout=subprocess.DEVNULL)
    if not python.exists():
        run(['uv', 'venv', '--python', '3.12', args.venv])
    lock = (Path(__file__).with_name('serving_sim_requirements_aarch64.lock')
            if platform.machine() == 'aarch64' else args.sim_root/'serving_sim/requirements.lock')
    run(['uv', 'pip', 'sync', '--python', python, '--require-hashes', lock])
    child = [python, '-m', 'clawbox.integrations.serving_sim', '_metadata',
             '--sim-root', args.sim_root, '--venv', args.venv, '--cores', args.cores]
    env = dict(os.environ, BUILD_JOBS=str(min(args.cores, 6)), JOBS=str(min(args.cores, 6)),
               PYO3_PYTHON=str(python), SIM_BUILD_CORES=str(args.cores),
               SIM_CPUSET=','.join(map(str, sorted(os.sched_getaffinity(0)))))
    run(['bash', args.sim_root/'serving_sim/native/build.sh'], env=env)
    run(child)
    run(['bash', args.sim_root/'serving_sim/routers/dynamo_shim_d66/build.sh'], env=env)
    source = Path(os.environ.get('MOONCAKE_SRC', Path.home()/'.cache/agent-sched-bench/mooncake-v0.3.13.post1')).expanduser()
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'clone', '--branch', 'v0.3.13.post1', '--recurse-submodules',
             'https://github.com/kvcache-ai/Mooncake.git', source])
    pin = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if pin != MOONCAKE_COMMIT:
        raise ValueError(f'MOONCAKE_SRC must be pinned to {MOONCAKE_COMMIT}; found {pin}')
    run(['git', '-C', source, 'diff', '--quiet', 'HEAD', '--'])
    run(['git', '-C', source, 'submodule', 'update', '--init', '--recursive'])
    env['MOONCAKE_SRC'] = str(source)
    run(['bash', args.sim_root/'serving_sim/engine/kv_pool/mooncake_shim/build.sh'], env=env)
    child[3] = '_check'
    run(child)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'check', 'smoke', 'serve', '_check', '_metadata'])
    parser.add_argument('--sim-root', required=True, type=Path, help='unmodified GPU simulator checkout')
    parser.add_argument('--venv', type=Path, default=Path(os.environ.get('SIM_VENV',
                        Path.home()/'.cache/clawbox/serving-sim/venv')), help='isolated simulator environment; reused if present')
    parser.add_argument('--cores', type=int, default=4, help='real allowed logical CPUs (default: 4)')
    parser.add_argument('--nofile', type=int, default=65536, help='minimum process open-file soft limit (default: 65536)')
    parser.add_argument('--diagnostic-interval-seconds', type=float, default=1.,
                        help='process and event-loop sample interval in seconds (default: 1)')
    parser.add_argument('--topology', default='8A')
    speed = parser.add_mutually_exclusive_group()
    speed.add_argument('--decode-time-scale', type=float,
                       help='positive finite decode-time multiplier; non-physical, default: calibrated timing')
    speed.add_argument('--tpot-ms', type=float,
                       help='positive finite target milliseconds per output token; non-physical, excludes decode-time-scale')
    parser.add_argument('--cpu-refill-mode', choices=['off', 'decode-first'], default='off',
                        help='CPU-aware decode-first policy; default: off')
    parser.add_argument('--forecast-interval-seconds', type=float, default=.5)
    parser.add_argument('--forecast-window-seconds', type=float, default=5.)
    parser.add_argument('--forecast-validity-seconds', type=float, default=2.)
    parser.add_argument('--cpu-refill-feedback-interval-seconds', type=float, default=1.)
    parser.add_argument('--cpu-refill-activation-floor-utilization', type=float, default=0.)
    parser.add_argument('--cpu-refill-enter-utilization', type=float, default=.90)
    parser.add_argument('--cpu-refill-exit-utilization', type=float, default=.95)
    parser.add_argument('--cpu-refill-enter-samples', type=int, default=2)
    parser.add_argument('--cpu-refill-stale-seconds', type=float, default=3.)
    parser.add_argument('--cpu-refill-throttle-steps', type=int, default=3)
    parser.add_argument('--cpu-refill-release-steps', type=int, default=1)
    parser.add_argument('--trace-catalog', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--persistence', choices=['native', 'final'], default='native',
                        help='both use native incremental input flushing and final summaries at graceful shutdown')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--k', type=float, default=1.)
    parser.add_argument('--v0', type=float, default=0.)
    parser.add_argument('--tokenizer-dir', type=Path)
    parser.add_argument('--api-key-env', default='CLAWBOX_SIM_API_KEY')
    parser.add_argument('--ssl-keyfile')
    parser.add_argument('--ssl-certfile')
    args = parser.parse_args(argv)
    try:
        import math
        for name in ('decode_time_scale', 'tpot_ms'):
            value = getattr(args, name)
            if value is not None:
                if args.command != 'serve':
                    raise ValueError(f'--{name.replace("_", "-")} applies to serve only')
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f'{name} must be positive and finite')
        if (not all(math.isfinite(value) for value in (
                args.cpu_refill_feedback_interval_seconds,
                args.cpu_refill_activation_floor_utilization,
                args.cpu_refill_enter_utilization,
                args.cpu_refill_exit_utilization,
                args.cpu_refill_stale_seconds,
            )) or not 0 <= args.cpu_refill_activation_floor_utilization
                < args.cpu_refill_enter_utilization
                < args.cpu_refill_exit_utilization <= 1
                or not 0 < args.cpu_refill_feedback_interval_seconds
                < args.cpu_refill_stale_seconds
                or min(args.cpu_refill_enter_samples, args.cpu_refill_throttle_steps,
                       args.cpu_refill_release_steps) < 1):
            raise ValueError('invalid cpu_refill hysteresis, interval or cadence')
        require_host()
        if (not all(math.isfinite(v) and v > 0 for v in (args.forecast_interval_seconds,
                args.forecast_window_seconds, args.forecast_validity_seconds))
                or args.forecast_interval_seconds >= args.forecast_validity_seconds):
            raise ValueError('forecast requires positive window and 0 < interval < validity')
        import resource
        if args.nofile < 1 or not math.isfinite(args.diagnostic_interval_seconds) or args.diagnostic_interval_seconds <= 0:
            raise ValueError('nofile and diagnostic interval must be positive and finite')
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and args.nofile > hard:
            raise ValueError(f'NOFILE hard limit {hard} is below {args.nofile}; raise LimitNOFILE or ulimit -Hn before launch')
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, args.nofile), hard))
        args.sim_root = args.sim_root.expanduser().resolve()
        args.venv = args.venv.expanduser().resolve()
        if not (args.sim_root/'serving_sim/setup.py').is_file():
            raise ValueError('--sim-root must point to the GPU simulator checkout')
        sys.path.insert(0, str(args.sim_root))
        os.environ['PYTHONPATH'] = os.pathsep.join([str(Path(__file__).resolve().parents[2]),
                                                   str(args.sim_root), os.environ.get('PYTHONPATH', '')])
        os.environ.update(PYTHONHASHSEED='0', CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='1',
                          OPENBLAS_NUM_THREADS='1', TOKENIZERS_PARALLELISM='false', SIM_VENV=str(args.venv))
        os.environ.setdefault('GLOO_SOCKET_IFNAME', 'lo')
        os.environ.setdefault('SERVING_SIM_CACHE', str(args.venv.parent/'cache'))
        os.environ.setdefault('FLASHINFER_WORKSPACE_BASE', str(Path(os.environ['SERVING_SIM_CACHE'])/'flashinfer'))
        from serving_sim.setup import cpu_budget
        cpu_budget(args.cores)
        python = args.venv/'bin/python'
        if args.command == 'setup':
            setup(args, python)
            return 0
        if args.command == '_metadata':
            metadata()
            return 0
        if args.command == '_check':
            from serving_sim.setup import check
            check()
            return 0
        if not python.exists():
            raise ValueError('simulator environment missing; run this command with setup first')
        # Reexec even when using the same interpreter: the deterministic hash
        # seed and thread budget must be present before native imports begin.
        if os.environ.pop('_CLAWBOX_SIM_CHILD', None) != '1':
            os.environ['_CLAWBOX_SIM_CHILD'] = '1'
            os.execv(str(python), [str(python), '-m', 'clawbox.integrations.serving_sim',
                                  *(argv if argv is not None else sys.argv[1:])])
        if args.command == 'check':
            from serving_sim.setup import check
            check()
        elif args.command == 'smoke':
            if args.out is None:
                raise ValueError('smoke requires --out NEW_DIRECTORY')
            from serving_sim.setup import smoke
            smoke(args.cores, args.out.resolve(), args.topology)
        else:
            if args.out is None or args.trace_catalog is None:
                raise ValueError('serve requires --out NEW_DIRECTORY and --trace-catalog FILE')
            if args.out.exists():
                raise FileExistsError(f'refusing to reuse simulator output directory {args.out}')
            import uvicorn
            from .serving_sim_app import create_app
            key = os.environ.get(args.api_key_env)
            app = create_app(args.out.resolve(), args.topology, args.trace_catalog.resolve(),
                             persistence=args.persistence,
                             diagnostic_interval_seconds=args.diagnostic_interval_seconds,
                             forecast_interval_seconds=args.forecast_interval_seconds,
                             forecast_window_seconds=args.forecast_window_seconds,
                             forecast_validity_seconds=args.forecast_validity_seconds,
                             v0=args.v0, k=args.k, tokenizer_dir=args.tokenizer_dir,
                             decode_time_scale=args.decode_time_scale, tpot_ms=args.tpot_ms,
                              cpu_refill_mode=args.cpu_refill_mode,
                              cpu_refill_feedback_interval_seconds=args.cpu_refill_feedback_interval_seconds,
                              cpu_refill_activation_floor_utilization=args.cpu_refill_activation_floor_utilization,
                              cpu_refill_enter_utilization=args.cpu_refill_enter_utilization,
                              cpu_refill_exit_utilization=args.cpu_refill_exit_utilization,
                              cpu_refill_enter_samples=args.cpu_refill_enter_samples,
                              cpu_refill_stale_seconds=args.cpu_refill_stale_seconds,
                              cpu_refill_throttle_steps=args.cpu_refill_throttle_steps,
                              cpu_refill_release_steps=args.cpu_refill_release_steps,
                             api_keys=[key] if key else None)
            uvicorn.run(app, host=args.host, port=args.port,
                        ssl_keyfile=args.ssl_keyfile, ssl_certfile=args.ssl_certfile)
        return 0
    except (ValueError, OSError, ImportError, subprocess.CalledProcessError) as exc:
        remedy = ('; run setup for this simulator checkout and --venv to build/install its native dependencies'
                  if isinstance(exc, ImportError) else '')
        print(f'ClawBox simulator {args.command} failed: {exc}{remedy}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
