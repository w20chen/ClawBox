#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from cubesandbox import Config, Sandbox


def trial(label: str, template: str, node: str, proxy: str | None) -> dict:
    owner = "clawbox-apt-install-poc-" + uuid.uuid4().hex
    sandbox = None
    row: dict = {"label": label, "owner": owner, "proxy": proxy}
    try:
        sandbox = Sandbox.create(
            template=template,
            timeout=-1,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.purpose": "apt-install-cache-poc"},
            distribution_scope=[node],
            allow_internet_access=True,
            config=Config(request_timeout=130),
        )
        row["sandbox_id"] = sandbox.sandbox_id
        before = sandbox.commands.run(
            "dpkg-query -W -f='${binary:Package}=${Version}\\n' python3-pip python3-pytest 2>&1 || true",
            timeout=30,
        )
        row["packages_before"] = before.stdout
        option = f"-o Acquire::http::Proxy={proxy} " if proxy else ""
        command = (
            "export DEBIAN_FRONTEND=noninteractive; "
            f"apt-get {option}-o APT::Update::Error-Mode=any update -qq && "
            f"apt-get {option}install -y -qq python3-pip python3-pytest"
        )
        started = time.monotonic()
        result = sandbox.commands.run(command, timeout=300)
        row.update({
            "elapsed_seconds": time.monotonic() - started,
            "exit_code": result.exit_code,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        })
        after = sandbox.commands.run(
            "dpkg-query -W -f='${binary:Package}=${Version}\\n' python3-pip python3-pytest",
            timeout=30,
        )
        row["packages_after"] = after.stdout
        row["packages_verified"] = after.exit_code == 0
    except BaseException as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
                row["cleanup_verified"] = True
            except BaseException as exc:
                row["cleanup_verified"] = False
                row["cleanup_error"] = f"{type(exc).__name__}: {exc}"
    return row


def main() -> int:
    output = Path(sys.argv[1])
    template, node, proxy = sys.argv[2:5]
    evidence = {
        "schema_version": 1,
        "command": "apt-get update -qq && apt-get install -y -qq python3-pip python3-pytest",
        "template_id": template,
        "node": node,
        "trials": [],
    }
    for label, selected_proxy in (
        ("direct", None),
        ("cache_package_cold", proxy),
        ("cache_package_warm", proxy),
    ):
        row = trial(label, template, node, selected_proxy)
        evidence["trials"].append(row)
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row, indent=2), flush=True)
    return 0 if all(
        row.get("exit_code") == 0
        and row.get("packages_verified")
        and row.get("cleanup_verified")
        for row in evidence["trials"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
