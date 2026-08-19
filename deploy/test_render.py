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


def test_tune_kb_deployment_has_persistent_non_root_storage_bootstrap() -> None:
    deployment = next(
        item for item in yaml.safe_load_all((HERE / "tune-kb.yaml").read_text(encoding="utf-8"))
        if item["kind"] == "Deployment"
    )
    pod = deployment["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["command"] == ["chown", "10001:10001", "/data"]
    assert init["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN"]}
    server = pod["containers"][0]
    assert server["securityContext"]["runAsNonRoot"] is True
    assert server["securityContext"]["runAsUser"] == 10001
    assert pod["volumes"][0]["hostPath"] == {
        "path": "/var/lib/clawbox/tune-kb", "type": "DirectoryOrCreate",
    }


def test_cell_controller_forwards_kb_endpoint_and_secret_refs() -> None:
    deployment = yaml.safe_load((HERE / "cell-controller.yaml").read_text(encoding="utf-8"))
    env = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CLAWBOX_KB_ENDPOINT"]["value"] == "http://clawbox-tune-kb.clawbox-system.svc:8086"
    assert env["CLAWBOX_KB_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "clawbox-control-plane", "key": "service-token",
    }
    assert env["CLAWBOX_KB_INGEST_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "clawbox-control-plane", "key": "ingest-secret",
    }
