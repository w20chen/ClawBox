from __future__ import annotations

import os
import sys
import time
import traceback

from clawbox.cell.capacity import KubernetesNodeCapacityProvider, SingleNodePlacementPolicy
from clawbox.cell.controller import CellReconciler, GROUP, PLURAL, VERSION
from clawbox.common.config import settings


def build_reconciler() -> CellReconciler:
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core = client.CoreV1Api()
    if settings.kubernetes_runtime_class != "kata-fc-arm64":
        raise RuntimeError("the Cell controller only supports kata-fc-arm64")
    node_api = client.NodeV1Api()
    runtime = node_api.read_runtime_class(settings.kubernetes_runtime_class)
    if runtime.handler != settings.kubernetes_runtime_class or not getattr(runtime.overhead, "pod_fixed", None):
        raise RuntimeError("kata-fc-arm64 handler and Pod overhead have not passed the host gate")
    tool_runtime = node_api.read_runtime_class(settings.kubernetes_tool_runtime_class)
    if (
        tool_runtime.handler != settings.kubernetes_tool_runtime_class
        or not getattr(tool_runtime.overhead, "pod_fixed", None)
    ):
        raise RuntimeError("Tool eBPF RuntimeClass has not passed the host gate")
    nodes = core.list_node(label_selector="clawbox.openai.com/firecracker-ready=true").items
    ready_arm64 = [node for node in nodes if (node.metadata.labels or {}).get("kubernetes.io/arch") == "arm64"]
    if len(ready_arm64) != 1:
        raise RuntimeError(f"exactly one audited arm64 Firecracker node is required; found {len(ready_arm64)}")
    node_name = ready_arm64[0].metadata.name
    return CellReconciler(
        core_api=core,
        batch_api=client.BatchV1Api(),
        networking_api=client.NetworkingV1Api(),
        custom_api=client.CustomObjectsApi(),
        capacity_provider=KubernetesNodeCapacityProvider(
            core, devmapper_available_bytes=int(os.getenv("CLAWBOX_DEVMAPPER_AVAILABLE_BYTES", "0")),
        ),
        placement_policy=SingleNodePlacementPolicy(node_name),
    )


def main() -> None:
    reconciler = build_reconciler()
    namespace = settings.cell_namespace
    interval = float(os.getenv("CLAWBOX_RECONCILE_INTERVAL_SECONDS", "2"))
    max_workload_starts = int(os.getenv("CLAWBOX_MAX_WORKLOAD_STARTS_PER_CYCLE", "1"))
    if max_workload_starts < 1:
        raise RuntimeError("CLAWBOX_MAX_WORKLOAD_STARTS_PER_CYCLE must be at least 1")
    while True:
        response = reconciler.custom.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL)
        starts_remaining = max_workload_starts
        for task in response.get("items", []):
            try:
                started = reconciler.reconcile(task, allow_workload_start=starts_remaining > 0)
                if started:
                    starts_remaining -= 1
            except Exception:
                name = task.get("metadata", {}).get("name", "unknown")
                print(f"reconcile failed task={name}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        time.sleep(max(0.2, interval))


if __name__ == "__main__":
    main()
