from __future__ import annotations

import json
import math
import re
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
    run_label,
)
from clawbox.cell.capacity import (
    AtomicAdmission,
    FixedProfileSizer,
    KubernetesNodeCapacityProvider,
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


def test_run_label_preserves_documented_selector_value_and_hashes_unsafe_input():
    assert run_label("swe-20260827t120000") == "swe-20260827t120000"
    assert run_label("Human Run") == dns_label("Human Run")


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
    sizer = FixedProfileSizer()
    size = sizer.size("small")
    # ClawTune runs in-process inside the Runtime container (single-container
    # Kata job), so its budget is merged into size.runtime; the reservation must
    # not double-count it as a phantom sidecar on top of the container requests.
    unsafed = size.runtime + size.tool + size.vm_overhead + size.vm_overhead
    assert size.reservation.cpu_millis > unsafed.cpu_millis
    assert size.reservation.memory_bytes > unsafed.memory_bytes
    assert size.reservation.storage_bytes > unsafed.storage_bytes
    assert size.reservation.pods == 2
    # Reservation is exactly the merged base scaled by the safety fraction.
    expected = ResourceVector(
        math.ceil(unsafed.cpu_millis * (1 + sizer.safety_fraction)),
        math.ceil(unsafed.memory_bytes * (1 + sizer.safety_fraction)),
        math.ceil(unsafed.storage_bytes * (1 + sizer.safety_fraction)),
        2,
    )
    assert size.reservation == expected

    admission = AtomicAdmission(size.reservation)
    assert admission.reserve("one", size.reservation)
    assert admission.reserve("one", size.reservation)  # idempotent
    assert not admission.reserve("two", ResourceVector(1, 1, 1, 1))


def test_runtime_manifest_and_reservation_account_for_inprocess_clawtune():
    """CBX-M0-002: admission reservation, Runtime request/limit and the design
    profile math must agree for every profile. The Runtime container (which
    hosts OpenClaw AND in-process ClawTune) requests exactly size.runtime; the
    reservation is the merged base + both VM overheads scaled by safety.
    """
    sizer = FixedProfileSizer()
    for profile in ("small", "medium", "large"):
        size = sizer.size(profile)
        base_runtime, base_tool = FixedProfileSizer.PROFILES[profile]
        # ClawTune budget is merged into the runtime budget, tool unchanged.
        assert size.runtime == base_runtime + size.sidecar
        assert size.tool == base_tool
        # Rendered Runtime Job requests exactly the merged runtime budget.
        job = runtime_job(task(), size)
        req = job["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
        assert req == {
            "cpu": f"{size.runtime.cpu_millis}m",
            "memory": str(size.runtime.memory_bytes),
            "ephemeral-storage": str(size.runtime.storage_bytes),
        }
        # Rendered Tool Pod requests exactly the tool budget.
        pod = tool_pod(task(), size)
        tool_req = pod["spec"]["containers"][0]["resources"]["requests"]
        assert tool_req["cpu"] == f"{size.tool.cpu_millis}m"
        # Reservation == merged base + both VM overheads, scaled by safety.
        merged = size.runtime + size.tool + size.vm_overhead + size.vm_overhead
        expected = ResourceVector(
            math.ceil(merged.cpu_millis * (1 + sizer.safety_fraction)),
            math.ceil(merged.memory_bytes * (1 + sizer.safety_fraction)),
            math.ceil(merged.storage_bytes * (1 + sizer.safety_fraction)),
            2,
        )
        assert size.reservation == expected


def test_runtime_uses_stable_tenant_for_kb_without_changing_cell_identity():
    value = task()
    value["metadata"]["labels"] = {"clawbox.openai.com/tenant": "tenant-a"}
    job = runtime_job(value, FixedProfileSizer().size("small"))
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["TENANT_ID"] == "cell-a"
    assert env["CLAWBOX_TENANT_ID"] == "tenant-a"

    value["apiVersion"] = "clawbox.openai.com/v1alpha2"
    value["spec"]["runRef"] = {"tenantID": "Exact Tenant", "runID": "run-a", "attemptID": "attempt-a"}
    job = runtime_job(value, FixedProfileSizer().size("small"))
    env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CLAWBOX_TENANT_ID"] == "Exact Tenant"
    assert env["CLAWBOX_RUN_ID"] == "run-a"
    assert env["CLAWBOX_ATTEMPT_ID"] == "attempt-a"
    assert env["CLAWTUNE_REVISION"] == "e91e60bc1e5f3209fbcf6091013fde96f217e2a7"


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


def test_node_capacity_inventory_is_node_scoped_paginated_and_bounded():
    def container(cpu: str, memory: str, storage: str = "0"):
        return SimpleNamespace(resources=SimpleNamespace(requests={
            "cpu": cpu, "memory": memory, "ephemeral-storage": storage,
        }))

    def pod(
        name: str,
        *,
        phase: str = "Running",
        labels: dict[str, str] | None = None,
        cpu: str = "250m",
        memory: str = "128Mi",
        storage: str = "1Gi",
    ):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, labels=labels or {}),
            status=SimpleNamespace(phase=phase),
            spec=SimpleNamespace(
                containers=[container(cpu, memory, storage)],
                init_containers=[],
                overhead={},
            ),
        )

    node = SimpleNamespace(
        metadata=SimpleNamespace(
            name="kunpeng-a",
            labels={"kubernetes.io/arch": "arm64"},
        ),
        spec=SimpleNamespace(unschedulable=False),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")],
            allocatable={"cpu": "8", "memory": "16Gi", "ephemeral-storage": "100Gi", "pods": "32"},
        ),
    )

    class Core:
        def __init__(self):
            self.pod_calls: list[dict[str, object]] = []

        def list_node(self, *, label_selector: str):
            assert label_selector == "clawbox.openai.com/firecracker-ready=true"
            return SimpleNamespace(items=[node])

        def list_pod_for_all_namespaces(self, **kwargs):
            self.pod_calls.append(kwargs)
            if kwargs.get("_continue") is None:
                return SimpleNamespace(
                    items=[
                        pod("api", cpu="500m", memory="1Gi", storage="2Gi"),
                        # Completed Pods no longer consume scheduler capacity.
                        pod("completed", phase="Succeeded", cpu="4", memory="8Gi"),
                    ],
                    metadata=SimpleNamespace(_continue="page-2"),
                )
            assert kwargs["_continue"] == "page-2"
            return SimpleNamespace(
                items=[
                    pod("database", cpu="1", memory="2Gi", storage="3Gi"),
                    # Cell Pods are represented by SandboxTask reservations.
                    pod("cell", labels={"app.kubernetes.io/name": "clawbox-cell"}),
                ],
                metadata=SimpleNamespace(_continue=None),
            )

    core = Core()
    capacity = KubernetesNodeCapacityProvider(
        core,
        devmapper_available_bytes=80 * 1024**3,
        pod_list_page_size=2,
    ).capacity()

    assert core.pod_calls == [
        {
            "field_selector": (
                "spec.nodeName=kunpeng-a,status.phase!=Succeeded,status.phase!=Failed"
            ),
            "limit": 2,
        },
        {
            "field_selector": (
                "spec.nodeName=kunpeng-a,status.phase!=Succeeded,status.phase!=Failed"
            ),
            "limit": 2,
            "_continue": "page-2",
        },
    ]
    assert capacity == ResourceVector(
        6_500,
        13 * 1024**3,
        80 * 1024**3,
        30,
    )


def test_node_capacity_does_not_list_pods_without_an_eligible_node():
    class Core:
        def list_node(self, *, label_selector: str):
            del label_selector
            return SimpleNamespace(items=[])

        def list_pod_for_all_namespaces(self, **kwargs):
            raise AssertionError(f"unexpected Pod list: {kwargs}")

    capacity = KubernetesNodeCapacityProvider(
        Core(), devmapper_available_bytes=1024,
    ).capacity()

    assert capacity == ResourceVector(0, 0, 0, 0)


def test_node_capacity_rejects_repeated_pagination_tokens():
    node = SimpleNamespace(
        metadata=SimpleNamespace(
            name="kunpeng-a", labels={"kubernetes.io/arch": "arm64"},
        ),
        spec=SimpleNamespace(unschedulable=False),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")],
            allocatable={"cpu": "8", "memory": "16Gi", "ephemeral-storage": "100Gi", "pods": "32"},
        ),
    )

    class Core:
        def list_node(self, *, label_selector: str):
            del label_selector
            return SimpleNamespace(items=[node])

        def list_pod_for_all_namespaces(self, **kwargs):
            del kwargs
            return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue="stuck"))

    provider = KubernetesNodeCapacityProvider(Core(), devmapper_available_bytes=0)
    with pytest.raises(RuntimeError, match="repeated a continuation token"):
        provider.capacity()


def test_tool_and_runtime_are_separate_firecracker_pods_with_least_privilege():
    """Lock the current single-container Kata reality (2026-08-18).

    The Tool Pod is a single container with the bridge baked into the task image
    (command /usr/local/bin/tool-bridge); the Runtime Job is a single container
    whose entrypoint starts ClawTune in-process. There are no init containers
    because this Kata substrate cannot share volumes across containers.
    """
    value = task()
    size = FixedProfileSizer().size("small")
    tool = tool_pod(value, size)
    job = runtime_job(value, size)
    tool_spec = tool["spec"]
    runtime_spec = job["spec"]["template"]["spec"]

    assert tool_spec["runtimeClassName"] == "kata-fc-arm64-ebpf"
    assert runtime_spec["runtimeClassName"] == "kata-fc-arm64"
    assert tool_spec["restartPolicy"] == "Never"
    assert runtime_spec["restartPolicy"] == "Never"

    # Tool Pod: single container, no init container bridge installer.
    assert "initContainers" not in tool_spec
    assert len(tool_spec["containers"]) == 1
    assert tool_spec["containers"][0]["name"] == "task"
    assert tool_spec["containers"][0]["image"] == DIGEST
    assert tool_spec["containers"][0]["command"] == ["/usr/local/bin/tool-bridge"]
    assert tool_spec["containers"][0]["ports"][0]["containerPort"] == 2222

    # Runtime Job: single container; ClawTune runs in-process (entrypoint
    # launches it as a background process), not as a restartable init container.
    assert "initContainers" not in runtime_spec
    assert len(runtime_spec["containers"]) == 1
    assert runtime_spec["containers"][0]["name"] == "runtime"
    assert runtime_spec["containers"][0]["command"] == ["/usr/local/bin/runtime-entrypoint"]

    # Guest root is a documented, probe-verified compatibility form: Kata's
    # agent writes Secret/ConfigMap volume data dirs with mode 0000, so a
    # non-root uid hits EACCES. The microVM is the isolation boundary; the
    # supervisor/non-root separation is a M2 work item (CBX-M2-001). Do not
    # pretend the containers are non-root.
    assert tool_spec["securityContext"]["runAsUser"] == 0
    assert runtime_spec["securityContext"]["runAsUser"] == 0
    assert tool_spec["containers"][0]["securityContext"] == {
        "readOnlyRootFilesystem": False,
        # CAP_SYS_ADMIN lets the tool-bridge remount the guest cgroup2 tree rw
        # and create per-execution cgroups for exact cpu/io accounting
        # (probe-verified feasible in-guest). The microVM is the isolation
        # boundary; process-tree collection still works without this cap.
        "capabilities": {"add": ["SYS_ADMIN", "NET_ADMIN", "NET_RAW", "SYS_PTRACE"]},
    }
    assert runtime_spec["containers"][0]["securityContext"] == {"readOnlyRootFilesystem": True}

    tool_text = str(tool_spec)
    assert "OPENAI_API_KEY" not in tool_text
    assert "llm-api-key" not in tool_text
    assert "trace-upload-token" not in tool_text
    for spec in (tool_spec, runtime_spec):
        assert spec["automountServiceAccountToken"] is False
        for volume in spec["volumes"]:
            assert "hostPath" not in volume
            assert "persistentVolumeClaim" not in volume
    assert tool["metadata"]["ownerReferences"][0]["uid"] == "uid-a"
    assert job["metadata"]["ownerReferences"][0]["uid"] == "uid-a"


def test_task_secret_volume_projections_keep_private_material_separate():
    """Single-container Secret projection: the Tool side gets only the
    authorized public key + SSH host keys, never the runtime's private key,
    the upload token or any LLM material."""
    value = task()
    credentials = generate_ssh_credentials("cell-a")
    secret = credential_secrets(value, credentials, 60)[0]
    assert "trace-upload-token" in secret["stringData"]
    assert "id_ed25519" in secret["stringData"]  # runtime's client private key
    tool = tool_pod(value, FixedProfileSizer().size("small"))
    runtime = runtime_job(value, FixedProfileSizer().size("small"))

    # Single-container layout: the Tool Pod has exactly one Secret volume.
    tool_volumes = tool["spec"]["volumes"]
    assert len(tool_volumes) == 1
    assert tool_volumes[0]["secret"]["secretName"] == "cell-a-auth"
    tool_items = {item["key"] for item in tool_volumes[0]["secret"]["items"]}
    assert tool_items == {"id_ed25519.pub", "ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"}
    assert "id_ed25519" not in tool_items
    assert "trace-upload-token" not in tool_items

    auth = next(v for v in runtime["spec"]["template"]["spec"]["volumes"] if v["name"] == "auth")
    runtime_items = {item["key"] for item in auth["secret"]["items"]}
    assert runtime_items == {"id_ed25519", "ssh_host_ed25519_key.pub"}
    assert "ssh_host_ed25519_key" not in runtime_items  # host private key stays on the Tool
    assert "trace-upload-token" not in runtime_items

    # LLM key never reaches the tool pod in any form.
    assert "OPENAI_API_KEY" not in str(tool["spec"])
    assert "llm-api-key" not in str(tool["spec"])


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

    @staticmethod
    def _delete(store, name, namespace, **kwargs):
        del kwargs
        if store.pop((namespace, name), None) is None:
            raise Missing()

    def read_namespaced_pod(self, name, namespace): return self._read(self.pods, name, namespace)
    def create_namespaced_pod(self, namespace, body): return self._create(self.pods, namespace, body)
    def read_namespaced_service(self, name, namespace): return self._read(self.services, name, namespace)
    def create_namespaced_service(self, namespace, body): return self._create(self.services, namespace, body)
    def read_namespaced_secret(self, name, namespace): return self._read(self.secrets, name, namespace)
    def create_namespaced_secret(self, namespace, body): return self._create(self.secrets, namespace, body)
    def read_namespaced_config_map(self, name, namespace): return self._read(self.configmaps, name, namespace)
    def create_namespaced_config_map(self, namespace, body): return self._create(self.configmaps, namespace, body)
    def delete_namespaced_pod(self, name, namespace, **kwargs): return self._delete(self.pods, name, namespace, **kwargs)
    def delete_namespaced_service(self, name, namespace, **kwargs): return self._delete(self.services, name, namespace, **kwargs)
    def delete_namespaced_secret(self, name, namespace, **kwargs): return self._delete(self.secrets, name, namespace, **kwargs)
    def delete_namespaced_config_map(self, name, namespace, **kwargs): return self._delete(self.configmaps, name, namespace, **kwargs)


class FakeBatch:
    def __init__(self): self.jobs = {}
    def read_namespaced_job(self, name, namespace): return FakeCore._read(self.jobs, name, namespace)
    def create_namespaced_job(self, namespace, body): return FakeCore._create(self.jobs, namespace, body)
    def delete_namespaced_job(self, name, namespace, **kwargs): return FakeCore._delete(self.jobs, name, namespace, **kwargs)


class FakeNetwork:
    def __init__(self): self.policies = {}
    def read_namespaced_network_policy(self, name, namespace):
        return FakeCore._read(self.policies, name, namespace)
    def create_namespaced_network_policy(self, namespace, body):
        return FakeCore._create(self.policies, namespace, body)
    def delete_namespaced_network_policy(self, name, namespace, **kwargs):
        return FakeCore._delete(self.policies, name, namespace, **kwargs)


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


def test_reconciler_stable_cleaned_phase_ignores_cancelled_desired_state():
    core, batch, network, custom = FakeCore(), FakeBatch(), FakeNetwork(), FakeCustom()
    reconciler = CellReconciler(
        core_api=core, batch_api=batch, networking_api=network, custom_api=custom,
        capacity_provider=StaticCapacityProvider(ResourceVector(100_000, 10**15, 10**15, 1000)),
    )
    cleaned = task(phase=CellPhase.CLEANED.value)
    cleaned["spec"]["desiredState"] = "Cancelled"
    assert reconciler.reconcile(cleaned) is False
    assert custom.statuses == []


def test_reconciler_cleanup_preserves_terminal_outcome_reason():
    core, batch, network, custom = FakeCore(), FakeBatch(), FakeNetwork(), FakeCustom()
    reconciler = CellReconciler(
        core_api=core, batch_api=batch, networking_api=network, custom_api=custom,
        capacity_provider=StaticCapacityProvider(ResourceVector(100_000, 10**15, 10**15, 1000)),
    )
    failed = task(phase=CellPhase.FAILED.value)
    failed["status"].update({"outcome": "Failed", "reason": "RuntimeFailed"})
    assert reconciler.reconcile(failed) is False
    assert custom.statuses[-1]["phase"] == "Cleaned"
    assert custom.statuses[-1]["outcome"] == "Failed"
    assert custom.statuses[-1]["reason"] == "RuntimeFailed"


def test_reconciler_workload_start_gate_defers_materialization_only():
    core, batch, network, custom = FakeCore(), FakeBatch(), FakeNetwork(), FakeCustom()
    reconciler = CellReconciler(
        core_api=core, batch_api=batch, networking_api=network, custom_api=custom,
        capacity_provider=StaticCapacityProvider(ResourceVector(100_000, 10**15, 10**15, 1000)),
    )
    admitted = task(phase=CellPhase.ADMITTED.value)
    admitted["status"]["reservation"] = FixedProfileSizer().size("small").reservation.as_status()
    assert reconciler.reconcile(admitted, allow_workload_start=False) is False
    assert core.pods == {}
    assert custom.statuses == []
    assert reconciler.reconcile(admitted, allow_workload_start=True) is True
    assert ("clawbox-benchmarks", "cell-a-tool") in core.pods


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
    containerd_service = (ROOT / "deploy" / "containerd-clawbox.service").read_text(encoding="utf-8")
    assert 'Environment="DM_DISABLE_UDEV=1"' in containerd_service
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "**/__pycache__" in dockerignore
    assert "**/*.py[cod]" in dockerignore


def test_readme_separates_one_time_bootstrap_from_daily_concurrent_runs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## New host: one-time installation" in readme
    assert "## Existing host: start and run tasks" in readme
    assert "repeat the disk bootstrap" in readme
    assert "--parallelism 8" in readme
    assert "--timeout-seconds 120" in readme
    assert "--command-timeout-seconds 120" in readme
    assert "deploy/containerd-clawbox.service" in readme
    assert "Do not restart containerd while task VMs are running" in readme


def test_runtime_inprocess_sidecar_uses_a_final_upload_handshake():
    """The ClawTune sidecar is an in-process background process started by
    runtime-entrypoint inside the single runtime container, and the
    `.runtime-complete -> final upload -> .upload-complete` contract still
    holds statically (behavioral coverage lives in the shell/E2E gates).
    """
    sidecar = (ROOT / "scripts" / "clawtune-sidecar-entrypoint.sh").read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    manifest = (ROOT / "clawbox" / "cell" / "manifests.py").read_text(encoding="utf-8")
    # In-process sidecar: the entrypoint launches it in the background and
    # waits on the completion markers, instead of a restartable init container.
    assert "/usr/local/bin/clawtune-sidecar-entrypoint" in runtime
    assert "SIDECAR_PID=$!" in runtime
    assert "runtime-entrypoint" in manifest
    assert '".runtime-complete"' in runtime or ".runtime-complete" in runtime
    assert '.runtime-complete' in sidecar and '--once --require-result' in sidecar
    assert '.upload-complete' in runtime and 'central artifact upload timed out' in runtime
    assert '"workspaceRoot": "/testbed"' in runtime
    assert "agent_deadline=$((SECONDS + task_timeout))" in runtime
    assert "task_timeout + 120" not in runtime
    assert 'timeout -k 2 10 ssh -p "${tool_port}"' in runtime
    assert "/usr/local/bin/native-kb-pull.py" in runtime
    assert "/usr/local/bin/native-shadow-report.py" in runtime
    pull = (ROOT / "scripts" / "native-kb-pull.py").read_text(encoding="utf-8")
    assert "/v1/kb/native-snapshot" in pull
    assert "ClauseResourceKB.from_json_obj" in pull
    assert "RuntimeToolResourceKB.from_json_obj" in pull
    assert "native snapshot pair digest mismatch" in pull
    dockerfile = (ROOT / "docker" / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert "CLAWBOX_REVISION" in dockerfile
    assert "_mvdan_adapter/build.sh" in dockerfile
    assert "native-shadow-probe.py" in dockerfile
    # No restartable init container anywhere in the rendered runtime Job.
    assert '"restartPolicy": "Always"' not in manifest


def test_runtime_entrypoint_clawtune_config_is_observe_only_and_fail_open():
    """Parse-level contract for the in-process ClawTune config rendered by
    runtime-entrypoint.sh: it must stay observe-only, hook-only, fail-open with
    cgroup/affinity/NUMA disabled (M0 red line; only M4/M5 re-enable them).
    This parses the actual JSON heredoc, not a grep.
    """
    runtime = (ROOT / "scripts" / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    match = re.search(
        r'cat >"\$\{STATE_DIR\}/openclaw\.patch\.json" <<EOF\n(.*?)\nEOF',
        runtime, re.DOTALL,
    )
    assert match, "runtime-entrypoint.sh must render openclaw.patch.json via heredoc"
    config = json.loads(match.group(1))
    clawtune = config["plugins"]["entries"]["clawtune"]["config"]
    assert clawtune["mode"] == "observe"
    assert clawtune["executionBackend"] == "hook-only"
    assert clawtune["failOpen"] is True
    assert clawtune["enableCgroup"] is False
    assert clawtune["enableAffinity"] is False
    assert clawtune["enableNuma"] is False
    assert clawtune["autoStartSidecar"] is False
    assert clawtune["sidecarCommand"] == ""
    assert config["tools"]["elevated"]["enabled"] is False


def test_cell_manifests_structural_contract_locks_security_fields():
    """Golden/structural contract for the rendered Tool Pod, Runtime Job,
    Secret and NetworkPolicies. Any change to a security-sensitive field must
    be an explicit, reviewed fixture change (CBX-M0-001).
    """
    value = task()
    size = FixedProfileSizer().size("small")
    tool = tool_pod(value, size)
    job = runtime_job(value, size)
    secret = credential_secrets(value, generate_ssh_credentials("cell-a"), 60)[0]
    policies = network_policies(value)

    # No API/privilege surface on either workload: no SA token, no host
    # namespaces, only emptyDir/ConfigMap/Secret volumes.
    assert tool["spec"]["runtimeClassName"] == "kata-fc-arm64-ebpf"
    assert job["spec"]["template"]["spec"]["runtimeClassName"] == "kata-fc-arm64"
    for spec in (tool["spec"], job["spec"]["template"]["spec"]):
        assert spec["automountServiceAccountToken"] is False
        assert spec["restartPolicy"] == "Never"
        assert "hostNetwork" not in spec and "hostPID" not in spec and "hostIPC" not in spec
        for volume in spec["volumes"]:
            assert "hostPath" not in volume
            assert "persistentVolumeClaim" not in volume
            assert "emptyDir" in volume or "configMap" in volume or "secret" in volume

    # The auth Secret is the only secret and both sides project explicit items.
    assert secret["metadata"]["name"] == "cell-a-auth"
    assert set(secret["stringData"]) == {
        "id_ed25519", "id_ed25519.pub",
        "ssh_host_ed25519_key", "ssh_host_ed25519_key.pub", "trace-upload-token",
    }
    tool_volume = tool["spec"]["volumes"][0]["secret"]
    assert tool_volume["secretName"] == "cell-a-auth"
    assert {item["key"] for item in tool_volume["items"]} == {
        "id_ed25519.pub", "ssh_host_ed25519_key", "ssh_host_ed25519_key.pub",
    }

    # Exactly 4 fail-closed NetworkPolicies, all owner-referenced.
    assert len(policies) == 4
    names = {p["metadata"]["name"] for p in policies}
    assert names == {
        "cell-a-default-deny", "cell-a-tool-ingress",
        "cell-a-runtime-egress", "cell-a-tool-egress",
    }
    assert all(p["metadata"]["ownerReferences"] for p in policies)
    default_deny = next(p for p in policies if p["metadata"]["name"].endswith("default-deny"))
    assert set(default_deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}


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
    assert "--range 0-0 --retry 4 --retry-all-errors" in bootstrap
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
