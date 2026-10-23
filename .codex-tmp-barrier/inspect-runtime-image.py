from __future__ import annotations

import sys
import shlex

from cubesandbox import Sandbox


sandbox = Sandbox.connect(sys.argv[1])
result = sandbox.commands.run(
    """set -eu
echo OPENCLAW
openclaw --version
echo KB_IMPORT
/opt/clawtune/venv/bin/python -c 'import clawtune_kb,jsonschema,referencing,rpds; print(clawtune_kb.__file__); print(jsonschema.__file__); print(referencing.__file__); print(rpds.__file__)'
echo PLUGIN
test -d /opt/clawtune/packages/clawtune-plugin && echo present
du -sh /opt/clawtune/packages/clawtune-plugin /opt/clawtune/venv/lib/python*/site-packages/clawtune_kb 2>/dev/null || true
""",
    timeout=30,
)
print(result.stdout)
print(result.stderr, file=sys.stderr)
if result.exit_code:
    raise SystemExit(result.exit_code)
if len(sys.argv) > 2:
    home = f"/state/openclaw/{sys.argv[2]}"
    if len(sys.argv) > 3 and sys.argv[3] == "backups":
        detail = sandbox.commands.run(
            "set -eu; "
            f"for file in {shlex.quote(home)}/.openclaw/.openclaw/openclaw.json "
            f"{shlex.quote(home)}/.openclaw/.openclaw/openclaw.json.bak*; do "
            "test -f \"$file\" || continue; echo ===\"$file\"; cat \"$file\"; done",
            timeout=30,
        )
        print(detail.stdout)
        print(detail.stderr, file=sys.stderr)
        raise SystemExit(detail.exit_code)
    detail = sandbox.commands.run(
        "set -eu; "
        f"echo HOME={shlex.quote(home)}; "
        f"find {shlex.quote(home)} -maxdepth 5 "
        r"\( -type f -o -type l \) -printf '%y %p -> %l\n' | sort; "
        f"echo CONFIG; find {shlex.quote(home)} -maxdepth 4 -type f "
        r"\( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) "
        "-print -exec sed -n '1,220p' {} \\;; "
        f"echo BACKUPS; for file in {shlex.quote(home)}/.openclaw/.openclaw/openclaw.json.bak*; do "
        "test -f \"$file\" || continue; echo ===\"$file\"; cat \"$file\"; done",
        timeout=30,
    )
    print(detail.stdout)
    print(detail.stderr, file=sys.stderr)
    raise SystemExit(detail.exit_code)
