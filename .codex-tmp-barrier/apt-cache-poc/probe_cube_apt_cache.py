#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from cubesandbox import Config, Sandbox


def run_trial(*, label: str, template: str, node: str, proxy: str | None) -> dict:
    owner = "clawbox-apt-cache-poc-" + uuid.uuid4().hex
    sandbox = None
    row: dict = {"label": label, "owner": owner, "proxy": proxy}
    try:
        create_started = time.monotonic()
        sandbox = Sandbox.create(
            template=template,
            timeout=-1,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.purpose": "apt-cache-poc"},
            distribution_scope=[node],
            allow_internet_access=True,
            config=Config(request_timeout=130),
        )
        row["sandbox_id"] = sandbox.sandbox_id
        row["create_seconds"] = time.monotonic() - create_started
        if proxy:
            proxy_host = proxy.removeprefix("http://").split(":", 1)[0]
            reachability = sandbox.commands.run(
                f"timeout 5 bash -c '</dev/tcp/{proxy_host}/3142'",
                timeout=10,
            )
            row["proxy_reachability"] = {
                "exit_code": reachability.exit_code,
                "stdout": reachability.stdout,
                "stderr": reachability.stderr,
            }
        command = (
            "rm -rf /var/lib/apt/lists/* && apt-get "
            "-o APT::Update::Error-Mode=any "
        )
        if proxy:
            command += f"-o Acquire::http::Proxy={proxy} "
        command += "update -qq"
        started = time.monotonic()
        result = sandbox.commands.run(command, timeout=300)
        row.update({
            "apt_update_seconds": time.monotonic() - started,
            "exit_code": result.exit_code,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        })
    except BaseException as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sandbox is not None:
            kill_started = time.monotonic()
            try:
                sandbox.kill()
                row["cleanup_verified"] = True
            except BaseException as exc:
                row["cleanup_verified"] = False
                row["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            row["cleanup_seconds"] = time.monotonic() - kill_started
    return row


def main() -> int:
    output = Path(sys.argv[1])
    template = sys.argv[2]
    node = sys.argv[3]
    proxy = sys.argv[4] if len(sys.argv) > 4 else "http://10.100.0.1:3142"
    evidence = {
        "schema_version": 1,
        "purpose": "compare direct, cold-cache, and warm-cache apt update in real CubeSandbox VMs",
        "template_id": template,
        "node": node,
        "trials": [],
    }
    for label, selected_proxy in (
        ("direct", None),
        ("cache_cold", proxy),
        ("cache_warm", proxy),
    ):
        evidence["trials"].append(run_trial(
            label=label, template=template, node=node, proxy=selected_proxy,
        ))
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence["trials"][-1], indent=2), flush=True)
    return 0 if all(
        trial.get("exit_code") == 0 and trial.get("cleanup_verified")
        for trial in evidence["trials"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
