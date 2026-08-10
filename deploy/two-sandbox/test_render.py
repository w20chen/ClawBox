from __future__ import annotations

import re
import json
from pathlib import Path
from string import Template

import yaml


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def render(*, runtime_class: str = "", tool_runtime_class: str = "", tool_egress: str = "") -> str:
    values = {
        "TENANT_ID": "tenant-a",
        "RUNTIME_ID": "runtime-tenant-a",
        "RUNTIME_IMAGE": "registry.example/runtime:test",
        "TOOL_IMAGE": "registry.example/tool:test",
        "LLM_SECRET_NAME": "tenant-a-llm",
        "LLM_EGRESS_CIDR": "203.0.113.10/32",
        "LLM_EGRESS_PORT": "443",
        "SSH_SECRET_NAME": "tenant-a-ssh",
        "RUNTIME_RUNTIME_CLASS_LINE": f"runtimeClassName: {runtime_class}" if runtime_class else "",
        "TOOL_RUNTIME_CLASS_LINE": f"runtimeClassName: {tool_runtime_class}" if tool_runtime_class else "",
        "TOOL_EGRESS_POLICY": tool_egress,
        "TOOL_EXEC_TIMEOUT_SECONDS": "300",
        "TOOL_PIDS_LIMIT": "128",
        "TOOL_CPU_REQUEST": "250m",
        "TOOL_CPU_LIMIT": "1",
        "TOOL_MEMORY_REQUEST": "256Mi",
        "TOOL_MEMORY_LIMIT": "1Gi",
        "TOOL_STORAGE_REQUEST": "256Mi",
        "TOOL_STORAGE_LIMIT": "1Gi",
        "RUNTIME_CPU_REQUEST": "500m",
        "RUNTIME_CPU_LIMIT": "2",
        "RUNTIME_MEMORY_REQUEST": "512Mi",
        "RUNTIME_MEMORY_LIMIT": "2Gi",
        "RUNTIME_STORAGE_REQUEST": "512Mi",
        "RUNTIME_STORAGE_LIMIT": "2Gi",
    }
    return Template((HERE / "cell.yaml").read_text(encoding="utf-8")).safe_substitute(values)


def load_docs(text: str) -> list[dict]:
    docs = list(yaml.safe_load_all(text))
    assert docs and all(isinstance(doc, dict) for doc in docs)
    return docs


def test_default_render_is_valid_and_has_two_pods() -> None:
    docs = load_docs(render())
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    assert {d["metadata"]["name"] for d in deployments} == {
        "claw-tenant-a-runtime",
        "claw-tenant-a-tool",
    }
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        assert "runtimeClassName" not in pod
        assert pod["automountServiceAccountToken"] is False
        container = pod["containers"][0]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert not any(m["mountPath"] == "/var/run/docker.sock" for m in container["volumeMounts"])


def test_runtime_classes_are_independent() -> None:
    docs = load_docs(render(runtime_class="kata-fc", tool_runtime_class="gvisor"))
    by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    assert by_name["claw-tenant-a-runtime"]["spec"]["template"]["spec"]["runtimeClassName"] == "kata-fc"
    assert by_name["claw-tenant-a-tool"]["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"


def test_secrets_and_network_boundaries() -> None:
    docs = load_docs(render())
    tool = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-tool"))
    runtime = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-runtime"))
    tool_env = tool["spec"]["template"]["spec"]["containers"][0]["env"]
    runtime_env = runtime["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not {"OPENAI_API_KEY", "OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_TOKEN"} & {e["name"] for e in tool_env}
    assert "OPENAI_API_KEY" in {e["name"] for e in runtime_env}
    policies = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert any(p["metadata"]["name"].endswith("default-deny") for p in policies)
    assert any(p["metadata"]["name"].endswith("tool-ingress") for p in policies)
    assert "203.0.113.10/32" in render()


def test_every_resource_is_tenant_correlated() -> None:
    for doc in load_docs(render()):
        labels = doc["metadata"]["labels"]
        assert labels["claw.openai.com/tenant-id"] == "tenant-a"
        assert labels["claw.openai.com/runtime-id"] == "runtime-tenant-a"


def test_tool_limits_and_readiness_are_declared() -> None:
    docs = load_docs(render())
    tool = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-tool"))
    container = tool["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["TOOL_EXEC_TIMEOUT_SECONDS"] == "300"
    assert env["TOOL_PIDS_LIMIT"] == "128"
    assert "ephemeral-storage" in container["resources"]["limits"]
    assert container["readinessProbe"]["exec"]["command"]

    runtime = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-runtime")
    )
    runtime_container = runtime["spec"]["template"]["spec"]["containers"][0]
    runtime_readiness = " ".join(runtime_container["readinessProbe"]["exec"]["command"])
    assert "claw-tenant-a-tool/2222" in runtime_readiness


def test_ssh_key_material_is_minimally_split() -> None:
    docs = load_docs(render())
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    tool_items = deployments["claw-tenant-a-tool"]["spec"]["template"]["spec"]["volumes"][-1]["secret"]["items"]
    runtime_items = deployments["claw-tenant-a-runtime"]["spec"]["template"]["spec"]["volumes"][-1]["secret"]["items"]
    tool_keys = {item["key"] for item in tool_items}
    runtime_keys = {item["key"] for item in runtime_items}
    assert "id_ed25519" not in tool_keys
    assert "ssh_host_ed25519_key" not in runtime_keys
    assert "id_ed25519.pub" in tool_keys
    assert "ssh_host_ed25519_key.pub" in runtime_keys


def test_no_unexpanded_template_variables() -> None:
    unresolved = re.findall(r"\$[A-Z][A-Z0-9_]*", render())
    assert unresolved == []


def test_shell_inputs_are_validated_before_render() -> None:
    script = (HERE / "cell.sh").read_text(encoding="utf-8")
    assert "valid_name" in script
    assert "valid_image" in script
    assert "valid_cidr" in script
    assert 'rm -rf -- "${tmp_dir}"' in script


def test_existing_kata_mode_is_untouched() -> None:
    assert (ROOT / "deploy" / "kata-firecracker" / "job.yaml").exists()
    assert (ROOT / "docker" / "Dockerfile.runner").exists()


def test_openclaw_example_is_json() -> None:
    config = json.loads((HERE / "openclaw-sandbox.example.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["sandbox"]["backend"] == "ssh"
    assert config["tools"]["exec"]["host"] == "sandbox"


def test_runtime_entrypoint_emits_valid_openclaw_patch() -> None:
    script = (ROOT / "scripts" / "two-sandbox" / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    match = re.search(r'cat >"\$\{STATE_DIR\}/openclaw\.patch\.json" <<EOF\n(.*?)\nEOF', script, re.S)
    assert match is not None
    rendered = Template(match.group(1)).safe_substitute(
        TOOL_SSH_TARGET="executor@claw-tenant-a-tool:2222",
        KNOWN_HOSTS="/state/tenant-a/ssh/known_hosts",
        SIDECAR_PORT="8765",
    )
    config = json.loads(rendered)
    assert config["agents"]["defaults"]["sandbox"]["backend"] == "ssh"
    assert config["tools"]["sandbox"]["tools"]["allow"] == [
        "exec", "process", "read", "write", "edit", "apply_patch"
    ]
    assert config["plugins"]["entries"]["agent-scheduler"]["config"]["executionBackend"] == "hook-only"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
