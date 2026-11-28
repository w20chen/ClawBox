"""Scope NUMA fault balancing to Cubelet and its future VM descendants."""
from __future__ import annotations

import json

from .preflight import command

UNIT = "cube-sandbox-cubelet"
DROP_IN = f"/etc/systemd/system/{UNIT}.service.d/90-clawbox-numa-policy.conf"


def state() -> dict:
    configured = command("systemctl", "show", UNIT, "-p", "NUMAPolicy", "--value")
    if configured == "n/a":
        configured = "default"
    pid = int(command("systemctl", "show", UNIT, "-p", "MainPID", "--value"))
    if pid <= 0:
        raise RuntimeError("Cubelet has no running MainPID; start the installed service")
    # The executable's ordinary mapping reports the task's inherited policy.
    # Inspect the live process too: daemon-reload alone does not change it.
    maps = command("sudo", "-n", "cat", f"/proc/{pid}/numa_maps")
    actual = maps.splitlines()[0].split()[1]
    return dict(configured=configured, actual=actual, pid=pid)


def verify(desired: str) -> dict:
    observed = state()
    if observed["configured"] != desired or observed["actual"] != desired:
        raise RuntimeError(
            f"Cubelet NUMA policy mismatch: requested={desired}, observed={observed}; "
            "run experiment host apply with cubelet_numa_policy on an idle VM pool"
        )
    return observed


def configure(desired: str | None) -> bool:
    """Persist an explicit choice; caller owns the idle check and restart."""
    if desired is None:
        return False
    if desired not in {"default", "local"}:
        raise ValueError("cubelet_numa_policy must be default or local")
    observed = state()
    if observed["configured"] == desired:
        return observed["actual"] != desired
    script = """
import pathlib, shutil, sys, time
p = pathlib.Path(sys.argv[1])
text = '[Service]\\nNUMAPolicy=' + sys.argv[2] + '\\n'
p.parent.mkdir(parents=True, exist_ok=True)
if p.exists():
    shutil.copy2(p, p.with_name(p.name + '.pre-clawbox-' + str(time.time_ns())))
p.write_text(text)
"""
    command("sudo", "-n", "python3", "-c", script, DROP_IN, desired)
    command("sudo", "-n", "systemctl", "daemon-reload")
    configured = command("systemctl", "show", UNIT, "-p", "NUMAPolicy", "--value")
    if configured != desired:
        raise RuntimeError(
            f"Cubelet NUMAPolicy did not load ({json.dumps(configured)}); "
            f"inspect systemd support and conflicting drop-ins for {UNIT}"
        )
    return True
