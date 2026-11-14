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
    run(child)
    env = dict(os.environ, BUILD_JOBS=str(min(args.cores, 6)), JOBS=str(min(args.cores, 6)),
               PYO3_PYTHON=str(python), SIM_BUILD_CORES=str(args.cores),
               SIM_CPUSET=','.join(map(str, sorted(os.sched_getaffinity(0)))))
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
    parser.add_argument('--topology', default='8A')
    parser.add_argument('--trace-catalog', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--persistence', choices=['native', 'final'], default='native',
                        help='native: cumulative reports per request; final: save at graceful shutdown')
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
        require_host()
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
                             v0=args.v0, k=args.k, tokenizer_dir=args.tokenizer_dir,
                             api_keys=[key] if key else None)
            uvicorn.run(app, host=args.host, port=args.port,
                        ssl_keyfile=args.ssl_keyfile, ssl_certfile=args.ssl_certfile)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'ClawBox simulator {args.command} failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
