from types import SimpleNamespace

from clawbox.benchmark.kubernetes import BenchmarkTask, KubernetesBenchmarkLauncher, render_job
from clawbox.controller.kubernetes_backend import KubernetesBackend, dns_label


def test_dns_label_is_stable_safe_and_collision_resistant():
    assert dns_label("Tenant A") == dns_label("Tenant A")
    assert dns_label("Tenant A") != dns_label("tenant-a")
    assert len(dns_label("x" * 200, prefix="tool-")) <= 63


def test_swe_job_uses_task_image_openclaw_bundle_and_kata_guaranteed_qos():
    job = render_job(
        BenchmarkTask("django__case-1", "swebench/task:latest", "fix it"),
        namespace="clawbox-benchmarks", bundle_image="registry/bundle:dev",
        llm_secret="llm", trace_pvc="traces",
    )
    pod = job["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "kata-qemu"
    assert pod["automountServiceAccountToken"] is False
    assert pod["initContainers"][0]["image"] == "registry/bundle:dev"
    assert pod["initContainers"][0]["imagePullPolicy"] == "IfNotPresent"
    assert "mkdir -p /trace-root/$TASK_INSTANCE_ID" in pod["initContainers"][0]["command"][-1]
    runtime = pod["containers"][0]
    assert runtime["image"] == "swebench/task:latest"
    assert runtime["command"] == ["/claw/entrypoint.sh"]
    assert runtime["resources"]["requests"] == runtime["resources"]["limits"]
    assert pod["initContainers"][0]["resources"]["requests"] == pod["initContainers"][0]["resources"]["limits"]
    traces = next(volume for volume in pod["volumes"] if volume["name"] == "traces")
    assert traces["persistentVolumeClaim"]["claimName"] == "traces"
    assert next(m for m in runtime["volumeMounts"] if m["name"] == "traces")["subPathExpr"] == "$(TASK_INSTANCE_ID)"


def test_swe_job_does_not_put_llm_secrets_in_plaintext():
    job = render_job(
        BenchmarkTask("case", "image", "problem"), namespace="bench",
        bundle_image="bundle", llm_secret="llm",
    )
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    secrets = {item["name"]: item["valueFrom"]["secretKeyRef"] for item in env if "valueFrom" in item}
    assert set(secrets) == {"LLM_API_KEY", "LLM_UPSTREAM_BASE_URL", "LLM_MODEL", "OPENCLAW_MODEL_REF"}


class FakeCore:
    def __init__(self):
        self.pods = []
        self.services = []
        self.namespaces = []

    def read_namespace(self, name):
        from kubernetes.client.exceptions import ApiException
        raise ApiException(status=404)

    def create_namespace(self, body):
        self.namespaces.append(body)

    def create_namespaced_pod(self, namespace, body):
        self.pods.append((namespace, body))
        return SimpleNamespace(metadata=SimpleNamespace(uid="physical-pod-uid"))

    def create_namespaced_service(self, namespace, body):
        self.services.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(
            phase="Running", container_statuses=[SimpleNamespace(ready=True)]
        ))

    def delete_namespaced_service(self, name, namespace):
        return None

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        return None


def test_controller_backend_creates_kata_service_and_guaranteed_tool_pod():
    from clawbox.common.models import ToolSpec

    core = FakeCore()
    created = KubernetesBackend(core).create(ToolSpec(
        tenant_id="tenant-a", execution_id="exec", workspace_id="workspace-a",
        cpu_count=4, memory_bytes=1024, image="registry/tool:dev",
    ), "logical-tool-id")
    assert created.pod_uid == "physical-pod-uid"
    assert created.endpoint.endswith(".svc:8090")
    pod = core.pods[0][1]["spec"]
    assert pod["runtimeClassName"] == "kata-qemu"
    resources = pod["containers"][0]["resources"]
    assert resources["requests"] == resources["limits"]
    assert core.services[0][1]["spec"]["selector"] == core.pods[0][1]["metadata"]["labels"]


def test_build_uses_generated_clawtune_bundle():
    from pathlib import Path

    root = Path(__file__).parents[1]
    script = (root / "scripts" / "build-kubernetes-images.sh").read_text(encoding="utf-8")
    dockerfile = (root / "docker" / "Dockerfile.clawtune-bundle").read_text(encoding="utf-8")
    assert "swe_rebench/.runtime/assets" in script
    assert "swe_rebench/.runtime/assets" in dockerfile
    assert "/bundle/sidecar" in dockerfile


def test_runtime_image_reuses_current_clawtune_v2_sources():
    from pathlib import Path

    root = Path(__file__).parents[1]
    dockerfile = (root / "docker" / "Dockerfile.runtime").read_text(encoding="utf-8")
    entrypoint = (root / "scripts" / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    assert "packages/clawtune-plugin" in dockerfile
    assert "services/sidecar" in dockerfile
    assert "clawtune_sidecar, tool_resource" in dockerfile
    assert "packages/openclaw-plugin" not in dockerfile
    assert "services/scheduler" not in dockerfile
    assert "clawtune_sidecar.main" in entrypoint
    assert "plugins enable clawtune" in entrypoint
    assert '"clawtune"' in entrypoint
    assert "agent_scheduler" not in entrypoint


def test_build_fails_fast_on_clawtune_contract_drift():
    from pathlib import Path

    root = Path(__file__).parents[1]
    validator = (root / "scripts" / "validate_clawtune_integration.py").read_text(encoding="utf-8")
    build = (root / "scripts" / "build-kubernetes-images.sh").read_text(encoding="utf-8")
    assert 'manifest.get("id") != "clawtune"' in validator
    assert 'scripts.get("clawtune-sidecar")' in validator
    assert "LEGACY_MARKERS" in validator
    assert "validate_clawtune_integration.py" in build


def test_benchmark_preflight_checks_secret_and_bound_trace_pvc():
    class Core:
        def read_namespace(self, name):
            return SimpleNamespace()

        def read_namespaced_secret(self, name, namespace):
            return SimpleNamespace(data={key: "encoded" for key in (
                "llm-api-key", "llm-upstream-base-url", "llm-model", "openclaw-model-ref"
            )})

        def read_namespaced_persistent_volume_claim(self, name, namespace):
            return SimpleNamespace(status=SimpleNamespace(phase="Bound"))

    launcher = KubernetesBenchmarkLauncher(core=Core(), batch=SimpleNamespace())
    launcher._preflight(namespace="bench", llm_secret="llm", trace_pvc="traces")


def test_local_runner_automates_the_complete_smoke_path():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "scripts" / "local-kata-swe.sh").read_text(encoding="utf-8")
    for required in (
        "swe_rebench.runner prepare",
        "Dockerfile.clawtune-bundle",
        "deploy/control-plane-rbac.yaml",
        "create secret generic",
        "runtimeClassName",
        "clawbox.benchmark.kubernetes",
        "kind load docker-image",
        "k3s ctr images import",
        "ctr -n k8s.io images import",
        "helm upgrade",
        "no runtime for",
        "minikube ssh -- test -r /dev/kvm",
        "--bootstrap-minikube",
        "--runtime-class",
        "apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils",
        "virt-host-validate qemu",
        "minikube start --driver=kvm2",
        "CLAWBOX_LIBVIRT_REEXEC=1",
        "exec sg libvirt",
        "configure_minikube_devmapper",
        "containerd.userDropIn=",
        "snapshotter devmapper was not found",
        "failed to get reader from content store",
        "verify_minikube_devmapper",
        "prepare_minikube_kata_images",
        'involvedObject.uid=${pod_uid}',
    ):
        assert required in script
    assert "involvedObject.name=kata-fc-smoke" not in script


def test_arm64_stage0_gate_checks_arch_isolation_network_and_cleanup():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "scripts" / "arm64-kata-smoke.sh").read_text(encoding="utf-8")
    for required in (
        "kubernetes.io/arch: arm64",
        "runtimeClassName: ${RUNTIME_CLASS}",
        "NetworkPolicy",
        "attacker reached the Tool service",
        "/proc/sys/kernel/random/boot_id",
        'kubectl delete namespace "${NAMESPACE}"',
    ):
        assert required in script


def test_openeuler_host_gate_is_read_only_and_runtime_class_aware():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "deploy" / "check-host.sh").read_text(encoding="utf-8")
    assert "/etc/os-release" in script
    assert "/sys/fs/cgroup/cgroup.controllers" in script
    assert 'kubectl get runtimeclass "${RUNTIME_CLASS}"' in script
    assert "arm64-kata-smoke.sh" in script
    assert "kubectl apply" not in script


def test_minikube_devmapper_setup_is_persistent_and_nondestructive():
    from pathlib import Path

    root = Path(__file__).parents[1]
    setup = (root / "scripts" / "minikube-devmapper.sh").read_text(encoding="utf-8")
    config = (root / "deploy" / "containerd-devmapper.toml").read_text(encoding="utf-8")
    assert '"$(dirname "${INSTALL_PATH}")"' in setup
    assert '"$(dirname "${SERVICE_PATH}")"' in setup
    assert "if [[ ! -e \"${DATA_DIR}/data\" ]]" in setup
    assert "if [[ ! -e \"${DATA_DIR}/meta\" ]]" in setup
    assert "Before=containerd.service kubelet.service" in setup
    assert "systemctl enable clawbox-devmapper.service" in setup
    assert "pool_name = 'clawbox-devpool'" in config
    assert "base_image_size = '10GB'" in config
    assert "discard_unpacked_layers = false" in config


def test_kata_bootstrap_images_are_unpacked_for_devmapper():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "scripts" / "minikube-prepare-kata-images.sh").read_text(encoding="utf-8")
    assert "containerd config dump" in script
    assert '$1 == "sandbox"' in script
    assert "--snapshotter devmapper" in script
    assert "docker.io/library/alpine:3.22" in script
    assert "registry.k8s.io/pause:" not in script
    assert "registry.cn-hangzhou.aliyuncs.com/google_containers/pause" in script
    assert 'images tag --force "${pause_source}" "${pause_image}"' in script
    assert "CLAWBOX_PAUSE_MIRROR" in script
