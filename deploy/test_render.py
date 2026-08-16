from pathlib import Path
from string import Template

import yaml


HERE = Path(__file__).resolve().parent


def test_sandbox_task_template_is_the_only_cell_submission() -> None:
    text = Template((HERE / "cell.yaml").read_text(encoding="utf-8")).safe_substitute({
        "TASK_NAME": "demo", "NAMESPACE": "clawbox-benchmarks",
        "TOOL_IMAGE": "registry/task@sha256:" + "a" * 64,
        "PROBLEM_STATEMENT": '"fix it"', "BASE_COMMIT": '"abc"', "HINT_TEXT": '""',
        "LLM_SECRET_NAME": "clawbox-llm", "LLM_EGRESS_CIDR": "203.0.113.10/32",
        "LLM_EGRESS_PORT": "443", "TOOL_EGRESS_CIDRS": "[]", "RESOURCE_PROFILE": "small",
        "TASK_TIMEOUT_SECONDS": "1800", "TOOL_EXEC_TIMEOUT_SECONDS": "300",
        "TOOL_OUTPUT_LIMIT_BYTES": "4194304",
    })
    manifest = yaml.safe_load(text)
    assert manifest["kind"] == "SandboxTask"
    assert manifest["spec"]["toolImage"].endswith("a" * 64)
    assert "runtimeImage" not in manifest["spec"]
