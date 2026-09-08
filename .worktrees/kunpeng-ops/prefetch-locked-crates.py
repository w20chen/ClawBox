import concurrent.futures
import os
from pathlib import Path
import tomllib
import urllib.request

source = Path('/home/weitianc/CubeSandbox-standalone-20260907/CubeShim/Cargo.lock')
cache = Path('/home/weitianc/.cache/clawbox-cargo/registry/cache/index.crates.io-6f17d22bba15001f')
packages = [p for p in tomllib.loads(source.read_text())['package']
            if p.get('source', '').startswith('registry+')]

def fetch(package):
    name, version = package['name'], package['version']
    target = cache / f'{name}-{version}.crate'
    if target.exists():
        return
    temporary = target.with_suffix('.prefetch')
    try:
        url = f'https://static.crates.io/crates/{name}/{name}-{version}.crate'
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        # Cargo itself verifies the registry checksum against the lockfile.
        os.replace(temporary, target)
        print('prefetched', name, version, flush=True)
    except Exception as exc:
        print('fetch failed', name, str(exc), flush=True)
        temporary.unlink(missing_ok=True)

with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
    list(executor.map(fetch, packages))
