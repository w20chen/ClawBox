#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from cubesandbox import Config, Sandbox


def trial(label: str, template: str, node: str, index_url: str | None) -> dict:
    owner = "clawbox-pypi-cache-poc-" + uuid.uuid4().hex
    sandbox = None
    row: dict = {"label": label, "owner": owner, "index_url": index_url}
    try:
        sandbox = Sandbox.create(
            template=template,
            timeout=-1,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.purpose": "pypi-cache-poc"},
            distribution_scope=[node],
            allow_internet_access=True,
            config=Config(request_timeout=130),
        )
        row["sandbox_id"] = sandbox.sandbox_id
        bootstrap = sandbox.commands.run(
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get -o Acquire::http::Proxy=http://193.124.7.2:3142 "
            "-o APT::Update::Error-Mode=any update -qq && "
            "apt-get -o Acquire::http::Proxy=http://193.124.7.2:3142 "
            "install -y -qq python3-pip",
            timeout=300,
        )
        row["bootstrap_exit_code"] = bootstrap.exit_code
        row["bootstrap_stderr_tail"] = bootstrap.stderr[-2000:]
        if bootstrap.exit_code:
            return row
        environment = ""
        if index_url:
            environment = (
                f"PIP_INDEX_URL={index_url} "
                "PIP_TRUSTED_HOST=193.124.7.2 "
            )
        command = (
            "rm -rf /tmp/clawbox-pypi-poc && "
            f"{environment}python3 -m pip download --no-deps "
            "--dest /tmp/clawbox-pypi-poc pytest==8.3.5"
        )
        started = time.monotonic()
        result = sandbox.commands.run(command, timeout=300)
        row.update({
            "elapsed_seconds": time.monotonic() - started,
            "exit_code": result.exit_code,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        })
        verify = sandbox.commands.run(
            "test -n \"$(find /tmp/clawbox-pypi-poc -type f -name 'pytest-8.3.5-*' -print -quit)\"",
            timeout=30,
        )
        row["package_verified"] = verify.exit_code == 0
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
    template, node, index_url = sys.argv[2:5]
    evidence = {
        "schema_version": 1,
        "command": "python3 -m pip download --no-deps pytest==8.3.5",
        "template_id": template,
        "node": node,
        "trials": [],
    }
    for label, selected_index in (
        ("direct", None),
        ("cache_cold", index_url),
        ("cache_warm", index_url),
    ):
        row = trial(label, template, node, selected_index)
        evidence["trials"].append(row)
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row, indent=2), flush=True)
    return 0 if all(
        row.get("exit_code") == 0
        and row.get("package_verified")
        and row.get("cleanup_verified")
        for row in evidence["trials"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
