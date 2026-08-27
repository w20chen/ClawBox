from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


GIB = 1024**3
POD_LIST_PAGE_SIZE = 50


@dataclass(frozen=True)
class ResourceVector:
    cpu_millis: int
    memory_bytes: int
    storage_bytes: int
    pods: int = 0

    def __add__(self, other: "ResourceVector") -> "ResourceVector":
        return ResourceVector(
            self.cpu_millis + other.cpu_millis,
            self.memory_bytes + other.memory_bytes,
            self.storage_bytes + other.storage_bytes,
            self.pods + other.pods,
        )

    def fits(self, capacity: "ResourceVector") -> bool:
        return (
            self.cpu_millis <= capacity.cpu_millis
            and self.memory_bytes <= capacity.memory_bytes
            and self.storage_bytes <= capacity.storage_bytes
            and self.pods <= capacity.pods
        )

    def as_status(self) -> dict[str, int]:
        return {
            "cpuMillis": self.cpu_millis,
            "memoryBytes": self.memory_bytes,
            "storageBytes": self.storage_bytes,
            "pods": self.pods,
        }


@dataclass(frozen=True)
class CellSize:
    profile: str
    # Runtime container budget; already includes the in-process ClawTune budget
    # (single-container Kata job — see roadmap §10.3).
    runtime: ResourceVector
    tool: ResourceVector
    # ClawTune budget that was merged into `runtime`; kept as a breakdown so
    # callers can reason about it, never reserved separately.
    sidecar: ResourceVector
    vm_overhead: ResourceVector
    reservation: ResourceVector


class CellSizer(Protocol):
    def size(self, profile: str) -> CellSize: ...


class NodeCapacityProvider(Protocol):
    def capacity(self) -> ResourceVector: ...


class PlacementPolicy(Protocol):
    def select_node(self, size: CellSize) -> str | None: ...


class FixedProfileSizer:
    """Conservative initial profiles; both VM overheads and safety are reserved."""

    PROFILES = {
        "small": (ResourceVector(1_000, 2 * GIB, 4 * GIB), ResourceVector(2_000, 4 * GIB, 12 * GIB)),
        "medium": (ResourceVector(2_000, 4 * GIB, 8 * GIB), ResourceVector(4_000, 8 * GIB, 24 * GIB)),
        "large": (ResourceVector(4_000, 8 * GIB, 16 * GIB), ResourceVector(8_000, 16 * GIB, 48 * GIB)),
    }

    def __init__(self, *, overhead: ResourceVector | None = None, safety_fraction: float = 0.10):
        if not 0 <= safety_fraction <= 0.5:
            raise ValueError("safety_fraction must be between 0 and 0.5")
        self.overhead = overhead or ResourceVector(250, 256 * 1024**2, 0, 1)
        self.safety_fraction = safety_fraction

    def size(self, profile: str) -> CellSize:
        try:
            runtime, tool = self.PROFILES[profile]
        except KeyError as exc:
            raise ValueError(f"unknown resource profile: {profile}") from exc
        # ClawTune runs in-process inside the Runtime container (Kata on this
        # host cannot share volumes across containers, so there is no sidecar
        # container). Its budget must therefore be part of the Runtime
        # container's real request/limit — the reservation adds it exactly once
        # (inside runtime), never as a phantom sidecar on top of the container
        # requests. CBX-M0-002.
        clawtune = ResourceVector(250, 512 * 1024**2, GIB)
        runtime = runtime + clawtune
        base = runtime + tool + self.overhead + self.overhead
        reservation = ResourceVector(
            math.ceil(base.cpu_millis * (1 + self.safety_fraction)),
            math.ceil(base.memory_bytes * (1 + self.safety_fraction)),
            math.ceil(base.storage_bytes * (1 + self.safety_fraction)),
            2,
        )
        return CellSize(profile, runtime, tool, clawtune, self.overhead, reservation)


class ClawTunePredictionSizer:
    """Stable extension point; prediction is deliberately disabled for observe-only."""

    def size(self, profile: str) -> CellSize:
        raise NotImplementedError("ClawTune prediction sizing is not enabled in observe-only mode")


class SingleNodePlacementPolicy:
    def __init__(self, node_name: str | None = None):
        self.node_name = node_name

    def select_node(self, size: CellSize) -> str | None:
        del size
        return self.node_name


class StaticCapacityProvider:
    def __init__(self, value: ResourceVector):
        self.value = value

    def capacity(self) -> ResourceVector:
        return self.value


class AtomicAdmission:
    def __init__(self, capacity: ResourceVector):
        self.capacity = capacity
        self.reservations: dict[str, ResourceVector] = {}

    @property
    def used(self) -> ResourceVector:
        value = ResourceVector(0, 0, 0, 0)
        for reservation in self.reservations.values():
            value += reservation
        return value

    def reserve(self, cell_id: str, request: ResourceVector) -> bool:
        existing = self.reservations.get(cell_id)
        if existing:
            return existing == request
        if not (self.used + request).fits(self.capacity):
            return False
        self.reservations[cell_id] = request
        return True

    def release(self, cell_id: str) -> None:
        self.reservations.pop(cell_id, None)


def parse_cpu(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def parse_bytes(value: str) -> int:
    units = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
        "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * multiplier)
    return int(value)


def _quantities(resources: object) -> ResourceVector:
    requests = getattr(resources, "requests", None) or {}
    return ResourceVector(
        parse_cpu(str(requests.get("cpu", "0"))),
        parse_bytes(str(requests.get("memory", "0"))),
        parse_bytes(str(requests.get("ephemeral-storage", "0"))),
        0,
    )


def pod_request(pod: object) -> ResourceVector:
    """Return the scheduler-style request for a non-Cell Pod.

    Restartable init containers are added to the steady-state sum; ordinary
    init containers contribute only the largest init phase, matching the
    Kubernetes sidecar scheduling model closely enough for admission safety.
    """
    spec = getattr(pod, "spec", None)
    regular = ResourceVector(0, 0, 0, 1)
    for container in getattr(spec, "containers", None) or []:
        regular += _quantities(getattr(container, "resources", None))
    restartable = ResourceVector(0, 0, 0, 0)
    init_max = ResourceVector(0, 0, 0, 0)
    for container in getattr(spec, "init_containers", None) or []:
        request = _quantities(getattr(container, "resources", None))
        if getattr(container, "restart_policy", None) == "Always":
            restartable += request
        else:
            init_max = ResourceVector(
                max(init_max.cpu_millis, request.cpu_millis),
                max(init_max.memory_bytes, request.memory_bytes),
                max(init_max.storage_bytes, request.storage_bytes),
                0,
            )
    steady = regular + restartable
    value = ResourceVector(
        max(steady.cpu_millis, init_max.cpu_millis),
        max(steady.memory_bytes, init_max.memory_bytes),
        max(steady.storage_bytes, init_max.storage_bytes),
        1,
    )
    overhead = getattr(spec, "overhead", None) or {}
    return value + ResourceVector(
        parse_cpu(str(overhead.get("cpu", "0"))),
        parse_bytes(str(overhead.get("memory", "0"))),
        parse_bytes(str(overhead.get("ephemeral-storage", "0"))),
        0,
    )


class KubernetesNodeCapacityProvider:
    def __init__(
        self,
        core_api,
        *,
        devmapper_available_bytes: int,
        pod_list_page_size: int = POD_LIST_PAGE_SIZE,
    ):
        if pod_list_page_size < 1:
            raise ValueError("pod_list_page_size must be positive")
        self.core = core_api
        self.devmapper_available_bytes = devmapper_available_bytes
        self.pod_list_page_size = pod_list_page_size

    def _pods_on_node(self, node_name: str):
        """Yield scheduled Pods without retaining an unbounded cluster list.

        Capacity is node-local and terminal Pods consume no scheduler
        capacity, so API-server field selectors avoid transferring objects
        that cannot affect this calculation. Kubernetes list pagination gives
        every page in one consistent snapshot while bounding the controller's
        live decoded-object set. Inventory errors deliberately propagate:
        admission must fail closed rather than use a partial calculation.
        """
        continue_token: str | None = None
        while True:
            requested_token = continue_token
            kwargs: dict[str, object] = {
                "field_selector": (
                    f"spec.nodeName={node_name},"
                    "status.phase!=Succeeded,status.phase!=Failed"
                ),
                "limit": self.pod_list_page_size,
            }
            if continue_token:
                kwargs["_continue"] = continue_token
            response = self.core.list_pod_for_all_namespaces(**kwargs)
            yield from response.items
            continue_token = getattr(response.metadata, "_continue", None) or None
            if continue_token is None:
                return
            if continue_token == requested_token:
                raise RuntimeError(
                    f"Kubernetes Pod pagination repeated a continuation token for node {node_name}"
                )

    def capacity(self) -> ResourceVector:
        cpu = memory = storage = pods = 0
        eligible_nodes: set[str] = set()
        for node in self.core.list_node(label_selector="clawbox.openai.com/firecracker-ready=true").items:
            labels = getattr(node.metadata, "labels", {}) or {}
            conditions = getattr(node.status, "conditions", []) or []
            ready = any(item.type == "Ready" and item.status == "True" for item in conditions)
            if labels.get("kubernetes.io/arch") != "arm64" or not ready or getattr(node.spec, "unschedulable", False):
                continue
            eligible_nodes.add(node.metadata.name)
            allocatable = node.status.allocatable or {}
            cpu += parse_cpu(allocatable.get("cpu", "0"))
            memory += parse_bytes(allocatable.get("memory", "0"))
            storage += parse_bytes(allocatable.get("ephemeral-storage", "0"))
            pods += int(allocatable.get("pods", "0"))
        # Kubelet allocatable excludes host reservations, but not the requests
        # of already scheduled control-plane/workload Pods.  Cell Pods are
        # represented by SandboxTask reservations and must not be subtracted a
        # second time.
        for node_name in sorted(eligible_nodes):
            for pod in self._pods_on_node(node_name):
                phase = getattr(getattr(pod, "status", None), "phase", "")
                if phase in {"Succeeded", "Failed"}:
                    continue
                labels = getattr(getattr(pod, "metadata", None), "labels", {}) or {}
                if labels.get("app.kubernetes.io/name") == "clawbox-cell":
                    continue
                request = pod_request(pod)
                cpu -= request.cpu_millis
                memory -= request.memory_bytes
                storage -= request.storage_bytes
                pods -= request.pods
        if self.devmapper_available_bytes > 0:
            storage = min(storage, self.devmapper_available_bytes)
        return ResourceVector(max(cpu, 0), max(memory, 0), max(storage, 0), max(pods, 0))
