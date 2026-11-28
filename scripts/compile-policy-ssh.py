"""Install the stdlib-only SSH shim once, avoiding compilation on every call."""
import py_compile
import shlex
import sys
from pathlib import Path


def install(source: Path, launcher: Path) -> None:
    bytecode = launcher.with_name(launcher.name + '.pyc').resolve()
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    # Compile and execute with the same guest interpreter. The shim uses only
    # the stdlib; site-package discovery is unnecessary for every Tool call.
    launcher.write_text(
        '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -S '
        + shlex.quote(str(bytecode)) + ' "$@"\n', encoding='utf-8')
    launcher.chmod(0o755)


if __name__ == '__main__':
    install(Path(sys.argv[1]), Path(sys.argv[2]))
