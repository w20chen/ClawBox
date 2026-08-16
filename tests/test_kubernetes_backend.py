from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.benchmark.kubernetes import (
    BenchmarkTask,
    KubernetesBenchmarkLauncher,
    load_arm64_mapping,
    render_sandbox_task,
    resolve_arm64_tasks,
)
from clawbox.cell.capacity import (
    AtomicAdmission,
    FixedProfileSizer,
    ResourceVector,
    StaticCapacityProvider,
    pod_request,
)
from clawbox.cell.controller import CellPhase, CellReconciler
from clawbox.cell.manifests import (
    credential_secrets,
    generate_ssh_credentials,
    network_policies,
    runtime_job,
    tool_pod,
)
from clawbox.controller.kubernetes_backend import dns_label
from clawbox.ingester.auth import create_upload_token, verify_upload_token


ROOT = Path(__file__).parents[1]
DIGEST = "registry.example.com/swe/task@sha256:" + "a" * 64


def task(*, phase: str | None = None) -> dict:
    value = {
        "apiVersion": "clawbox.openai.com/v1alpha1",
        "kind": "SandboxTask",
        "metadata": {
            "name": "cell-a", "namespace": "clawbox-benchmarks", "uid": "uid-a",
            "generation": 1, "finalizers": ["clawbox.openai.com/cell-cleanup"],
        },
        "spec": {
            "toolImage": DIGEST,
            "problemStatement": "fix it",
            "llmSecretName": "clawbox-llm",
            "llmEgressCIDR": "203.0.113.10/32",
            "profile": "small",
            "timeoutSeconds": 1800,
        },
    }
    if phase:
        value["status"] = {"phase": phase, "admittedAt": "2099-01-01T00:00:00Z"}
    return value


def test_dns_label_is_stable_safe_and_collision_resistant():
    assert dns_label("Tenant A") == dns_label("Tenant A")
    assert dns_label("Tenant A") != dns_label("tenant-a")
    assert len(dns_label("x" * 200, prefix="tool-")) <= 63


def test_launcher_requires_a_supported_immutable_arm64_mapping(tmp_path: Path):
    mapping = tmp_path / "map.json"
    mapping.write_text(
        '{"upstream:latest":{"status":"supported","platform":"linux/arm64",'
        '"recipe_revision":"recipe-v1","arm64_image":"' + DIGEST + '"}}',
        encoding="utf-8",
    )
    resolved = resolve_arm64_tasks(
        [BenchmarkTask("case", "upstream:latest", "fix")], load_arm64_mapping(mapping),
    )
    assert resolved[0].image == DIGEST

    with pytest.raises(ValueError, match="no fallback"):
        resolve_arm64_tasks([BenchmarkTask("missing", "foreign:latest", "fix")], {})


def test_benchmark_renders_only_a_sandbox_task_cr():
    manifest = render_sandbox_task(
        BenchmarkTask("case", DIGEST, "fix"), namespace="clawbox-benchmarks",
        llm_secret="clawbox-llm", llm_egress_cidr="203.0.113.10/32",
    )
    assert manifest["kind"] == "SandboxTask"
    assert manifest["spec"]["toolImage"] == DIGEST
    assert "runtimeImage" not in manifest["spec"]
    assert "apiKey" not in str(manifest)
    assert len(manifest["metadata"]["name"] + "-runtime-egress") <= 63


def test_launcher_preflight_requires_firecracker_handler_overhead_and_crd():
    class Core:
        def read_namespace(self, name):
            return SimpleNamespace(metadata=SimpleNamespace(name=name))

        def read_namespaced_secret(self, name, namespace):
            del name, namespace
            return SimpleNamespace(data={key: "encoded" for key in (
                "llm-api-key", "llm-upstream-base-url", "llm-model", "openclaw-model-ref",
            )})

    class Node:
        def read_runtime_class(self, name):
            return SimpleNamespace(handler=name, overhead=SimpleNamespace(pod_fixed={"cpu": "250m"}))

    class Custom:
        def list_namespaced_custom_object(self, *args, **kwargs):
            del args, kwargs
            return {"items": []}

    launcher = KubernetesBenchmarkLauncher(core=Core(), custom=Custom(), node_api=Node())
    launcher._preflight(
        namespace="clawbox-benchmarks", llm_secret="clawbox-llm",
        runtime_class="kata-fc-arm64",
    )
    with pytest.raises(RuntimeError, match="only accepts"):
        launcher._preflight(
            namespace="clawbox-benchmarks", llm_secret="clawbox-llm",
            runtime_class="another-handler",
        )


def test_cell_profiles_reserve_both_vms_sidecar_overheads_and_safety():
    size = FixedProfileSizer().size("small")
    unsafed = size.runtime + size.tool + size.sidecar + size.vm_overhead + size.vm_overhead
    assert size.reservation.cpu_millis > unsafed.cpu_millis
    assert size.reservation.memory_bytes > unsafed.memory_bytes
    assert size.reservation.storage_bytes > unsafed.storage_bytes
    assert size.reservation.pods == 2

    admission = AtomicAdmission(size.reservation)
    assert admission.reserve("one", size.reservation)
    assert admission.reserve("one", size.reservation)  # idempotent
    assert not admission.reserve("two", ResourceVector(1, 1, 1, 1))


def test_non_cell_pod_request_counts_native_sidecar_and_runtime_overhead():
    def container(cpu: str, memory: str, *, restart: str | None = None):
        return SimpleNamespace(
            resources=SimpleNamespace(requests={"cpu": cpu, "memory": memory}),
            restart_policy=restart,
        )

    pod = SimpleNamespace(spec=SimpleNamespace(
        containers=[container("1", "1Gi")],
        init_containers=[container("250m", "256Mi", restart="Always"), container("2", "512Mi")],
        overhead={"cpu": "100m", "memory": "64Mi"},
    ))
    request = pod_request(pod)
    assert request.cpu_millis == 2100  # max(1.25 steady, 2 init) + overhead
    assert request.memory_bytes == (1024 + 256 + 64) * 1024**2
    assert request.pods == 1


def test_tool_and_runtime_are_separate_firecracker_pods_with_least_privilege():
    value = task()
    size = FixedProfileSizer().size("small")
    tool = tool_pod(value, size)
    job = runtime_job(value, size)
    tool_spec = tool["spec"]
    runtime_spec = job["spec"]["template"]["spec"]

    assert tool_spec["runtimeClassName"] == "kata-fc-arm64"
    assert runtime_spec["runtimeClassName"] == "kata-fc-arm64"
    assert tool_spec["containers"][0]["image"] == DIGEST
    assert tool_spec["initContainers"][0]["name"] == "install-tool-bridge"
    assert tool_spec["containers"][0]["ports"][0]["containerPort"] == 2222
    assert runtime_spec["initContainers"][0]["restartPolicy"] == "Always"

    sidecar_env = {item["name"]: item.get("value") for item in runtime_spec["initContainers"][0]["env"]}
    assert sidecar_env["CLAWTUNE_POLICY"] == "observe-only"
    assert sidecar_env["CLAWTUNE_EXECUTION_BACKEND"] == "hook-only"
    assert sidecar_env["CLAWTUNE_ENABLE_CGROUP"] == "false"
    assert sidecar_env["CLAWTUNE_ENABLE_AFFINITY"] == "false"
    assert sidecar_env["CLAWTUNE_ENABLE_NUMA"] == "false"

    tool_text = str(tool_spec)
    assert "OPENAI_API_KEY" not in tool_text
    assert "trace-upload-token" not in tool_text
    for volume in tool_spec["volumes"] + runtime_spec["volumes"]:
        assert "hostPath" not in volume
        assert "persistentVolumeClaim" not in volume
    assert tool["metadata"]["ownerReferences"][0]["uid"] == "uid-a"
    assert job["metadata"]["ownerReferences"][0]["uid"] == "uid-a"


def test_task_secret_volume_projections_keep_private_material_separate():
    value = task()
    credentials = generate_ssh_credentials("cell-a")
    secret = credential_secrets(value, credentials, 60)[0]
    assert "trace-upload-token" in secret["stringData"]
    tool = tool_pod(value, FixedProfileSizer().size("small"))
    runtime = runtime_job(value, FixedProfileSizer().size("small"))
    tool_items = {item["key"] for item in tool["spec"]["volumes"][1]["secret"]["items"]}
    runtime_items = {
        item["key"] for item in runtime["spec"]["template"]["spec"]["volumes"][-1]["secret"]["items"]
    }
    assert "id_ed25519" not in tool_items
    assert "trace-upload-token" not in tool_items
    assert "ssh_host_ed25519_key" not in runtime_items


def test_cell_network_policy_is_default_deny_and_task_scoped():
    policies = network_policies(task())
    names = {item["metadata"]["name"] for item in policies}
    assert names == {
        "cell-a-default-deny", "cell-a-tool-ingress",
        "cell-a-runtime-egress", "cell-a-tool-egress",
    }
    assert all(item["metadata"]["ownerReferences"] for item in policies)
    runtime = next(item for item in policies if item["metadata"]["name"].endswith("runtime-egress"))
    assert "203.0.113.10/32" in str(runtime)
    tool = next(item for item in policies if item["metadata"]["name"].endswith("tool-egress"))
    assert "203.0.113.10/32" not in str(tool)


class Missing(Exception):
    status = 404


class FakeCore:
    def __init__(self):
        self.pods = {}
        self.services = {}
        self.secrets = {}
        self.configmaps = {}

    @staticmethod
    def _read(store, name, namespace):
        try:
            return store[(namespace, name)]
        except KeyError as exc:
            raise Missing() from exc

    @staticmethod
    def _create(store, namespace, body):
        store[(namespace, body["metadata"]["name"])] = body
        return body

    def read_namespaced_pod(self, name, namespace): return self._read(self.pods, name, namespace)
    def create_namespaced_pod(self, namespace, body): return self._create(self.pods, namespace, body)
    def read_namespaced_service(self, name, namespace): return self._read(self.services, name, namespace)
    def create_namespaced_service(self, namespace, body): return self._create(self.services, namespace, body)
    def read_namespaced_secret(self, name, namespace): return self._read(self.secrets, name, namespace)
    def create_namespaced_secret(self, namespace, body): return self._create(self.secrets, namespace, body)
    def read_namespaced_config_map(self, name, namespace): return self._read(self.configmaps, name, namespace)
    def create_namespaced_config_map(self, namespace, body): return self._create(self.configmaps, namespace, body)


class FakeBatch:
    def __init__(self): self.jobs = {}
    def read_namespaced_job(self, name, namespace): return FakeCore._read(self.jobs, name, namespace)
    def create_namespaced_job(self, namespace, body): return FakeCore._create(self.jobs, namespace, body)


class FakeNetwork:
    def __init__(self): self.policies = {}
    def read_namespaced_network_policy(self, name, namespace):
        return FakeCore._read(self.policies, name, namespace)
    def create_namespaced_network_policy(self, namespace, body):
        return FakeCore._create(self.policies, namespace, body)


class FakeCustom:
    def __init__(self): self.statuses = []
    def list_namespaced_custom_object(self, *args, **kwargs):
        del args, kwargs
        return {"items": []}
    def patch_namespaced_custom_object_status(self, *args): self.statuses.append(args[-1]["status"])
    def patch_namespaced_custom_object(self, *args): pass


def test_reconciler_waits_for_tool_readiness_before_creating_runtime():
    core, batch, network, custom = FakeCore(), FakeBatch(), FakeNetwork(), FakeCustom()
    reconciler = CellReconciler(
        core_api=core, batch_api=batch, networking_api=network, custom_api=custom,
        capacity_provider=StaticCapacityProvider(ResourceVector(100_000, 10**15, 10**15, 1000)),
    )
    admitted = task(phase=CellPhase.ADMITTED.value)
    admitted["status"]["reservation"] = FixedProfileSizer().size("small").reservation.as_status()
    reconciler.reconcile(admitted)
    assert ("clawbox-benchmarks", "cell-a-tool") in core.pods
    assert batch.jobs == {}
    assert custom.statuses[-1]["phase"] == "ToolStarting"

    starting = task(phase=CellPhase.TOOL_STARTING.value)
    starting["status"]["reservation"] = admitted["status"]["reservation"]
    core.pods[("clawbox-benchmarks", "cell-a-tool")] = SimpleNamespace(status=SimpleNamespace(
        phase="Running", container_statuses=[SimpleNamespace(ready=True)],
    ))
    reconciler.reconcile(starting)
    assert custom.statuses[-1]["phase"] == "ToolReady"
    assert batch.jobs == {}

    ready = task(phase=CellPhase.TOOL_READY.value)
    ready["status"]["reservation"] = admitted["status"]["reservation"]
    reconciler.reconcile(ready)
    assert ("clawbox-benchmarks", "cell-a-runtime") in batch.jobs
    assert custom.statuses[-1]["phase"] == "RuntimeRunning"


def test_reconciler_refuses_to_adopt_a_preexisting_unowned_child():
    core, batch, network, custom = FakeCore(), FakeBatch(), FakeNetwork(), FakeCustom()
    core.secrets[("clawbox-benchmarks", "cell-a-auth")] = {
        "metadata": {"name": "cell-a-auth", "ownerReferences": []},
        "stringData": {"id_ed25519": "attacker-controlled"},
    }
    reconciler = CellReconciler(
        core_api=core, batch_api=batch, networking_api=network, custom_api=custom,
        capacity_provider=StaticCapacityProvider(ResourceVector(100_000, 10**15, 10**15, 1000)),
    )
    admitted = task(phase=CellPhase.ADMITTED.value)
    admitted["status"]["reservation"] = FixedProfileSizer().size("small").reservation.as_status()
    with pytest.raises(RuntimeError, match="refusing to adopt"):
        reconciler.reconcile(admitted)


def test_upload_tokens_are_task_scoped_signed_and_expiring():
    token = create_upload_token("cell-a", "secret", expires_at=int(time.time()) + 60)
    verify_upload_token(token, "cell-a", "secret")
    with pytest.raises(ValueError, match="another task"):
        verify_upload_token(token, "cell-b", "secret")
    expired = create_upload_token("cell-a", "secret", expires_at=int(time.time()) - 1)
    with pytest.raises(ValueError, match="expired"):
        verify_upload_token(expired, "cell-a", "secret")


def test_firecracker_host_and_image_gates_are_fail_closed():
    audit = (ROOT / "scripts" / "audit-kata-firecracker-arm64.sh").read_text(encoding="utf-8")
    builder = (ROOT / "scripts" / "build-kata-firecracker-arm64.sh").read_text(encoding="utf-8")
    storage = (ROOT / "scripts" / "setup-devmapper-openeuler-arm64.sh").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "arm64-kata-smoke.sh").read_text(encoding="utf-8")
    image_factory = (ROOT / "clawbox" / "images" / "arm64.py").read_text(encoding="utf-8")
    for required in ("Firecracker version is pinned", "aarch64 ELF", "block rootfs image", "explicit hypervisor path"):
        assert required in audit
    assert "static_sandbox_resource_mgmt" in audit
    assert "guest-local emptyDir" in audit
    assert 'kata_asset="kata-static-${KATA_VERSION}-arm64.tar.zst"' in builder
    assert "42a7e67a2c2bf3e97a615c99a293b2bc01ea9c84111fc2bf4abeedb7adc9c2ac" in builder
    assert "kata-go-static" not in builder
    for required in ("loopback devices are forbidden", "--confirm-erase", "backs a protected filesystem"):
        assert required in storage
    for required in ("/proc/sys/kernel/random/boot_id", "NetworkPolicy", "snapshot", "Firecracker"):
        assert required in smoke
    assert "foreign-architecture binfmt handlers must be disabled" in image_factory
    assert "linux/arm64" in image_factory


def test_native_sidecar_uses_a_final_upload_handshake():
    sidecar = (ROOT / "scripts" / "clawtune-sidecar-entrypoint.sh").read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    manifest = (ROOT / "clawbox" / "cell" / "manifests.py").read_text(encoding="utf-8")
    assert '.runtime-complete' in sidecar and '--once --require-result' in sidecar
    assert '.upload-complete' in runtime and 'central artifact upload timed out' in runtime
    assert '"workspaceRoot": "/testbed"' in runtime
    assert '"restartPolicy": "Always"' in manifest


def test_production_deployment_has_only_firecracker_runtimeclass():
    runtime_class = (ROOT / "deploy" / "runtimeclass-firecracker.yaml").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts" / "bootstrap-openeuler-arm64.sh").read_text(encoding="utf-8")
    assert "name: kata-fc-arm64" in runtime_class
    assert "handler: kata-fc-arm64" in runtime_class
    assert "podFixed:" in runtime_class
    assert 'RUNTIME_CLASS="${KUBERNETES_RUNTIME_CLASS:-kata-fc-arm64}"' in bootstrap
    assert 'STATE_SCHEMA_VERSION="2"' in bootstrap
    assert "completing incomplete pre-stage0 state" in bootstrap
    assert "migrating uninitialized QEMU state to Firecracker" in bootstrap
    assert '"${installed_runtime}" == kata-qemu-runtime-rs' in bootstrap
    assert 'migrated+=("${key}:${installed}->${requested}")' in bootstrap
    assert '! sudo test -s "${ADMIN_CONF}"' in bootstrap
    assert 'validate_storage_devices' in bootstrap
    assert "kubeadm reset" not in bootstrap


def test_scale_sequence_and_static_bridge_contract_are_present():
    scale = (ROOT / "scripts" / "scale-swe-rebench.sh").read_text(encoding="utf-8")
    bridge = (ROOT / "toolbridge" / "main.go").read_text(encoding="utf-8")
    dockerfile = (ROOT / "docker" / "Dockerfile.tool-bridge").read_text(encoding="utf-8")
    assert 'STEPS="${CLAWBOX_SCALE_STEPS:-1 2 4 8 16 32}"' in scale
    for field in ("CommandSHA256", "DurationMS", "ExitCode", "TimedOut", "MaxRSSKiB"):
        assert field in bridge
    assert 'metadata.User() != "executor"' in bridge
    assert "CGO_ENABLED=0 GOOS=linux GOARCH=arm64" in dockerfile
