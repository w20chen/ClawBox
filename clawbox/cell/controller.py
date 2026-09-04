from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

from clawbox.cube import CubeSandboxClient
from clawbox.experiments import ExperimentSpec

from .worker_manifest import (
    experiment_configmap, experiment_worker_job, resolved_spec_digest,
    worker_bridge_service,
)

GROUP = "clawbox.openai.com"
VERSION = "v1alpha2"
PLURAL = "sandboxtasks"
FINALIZER = "clawbox.openai.com/cube-cleanup"


class CellPhase(StrEnum):
    QUEUED = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "failed"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"


TERMINAL = {CellPhase.SUCCEEDED, CellPhase.FAILED, CellPhase.TIMED_OUT,
            CellPhase.CANCELLED, CellPhase.CLEANED}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_task(task: dict[str, Any]) -> ExperimentSpec:
    metadata = task.get("metadata") or {}
    spec = task.get("spec") or {}
    if len(str(metadata.get("name", ""))) > 48:
        raise ValueError("SandboxTask name must be at most 48 characters")
    for field in ("runRef", "workerImage", "cubeApiURL", "resultHostPath", "experimentSpec"):
        if not spec.get(field):
            raise ValueError(f"spec.{field} is required")
    return ExperimentSpec.model_validate(spec["experimentSpec"])


class CellReconciler:
    def __init__(self, *, core_api, batch_api, custom_api, networking_api=None,
                 cube_client: CubeSandboxClient | None = None, **_ignored: Any) -> None:
        self.core = core_api
        self.batch = batch_api
        self.custom = custom_api
        self.cube = cube_client or CubeSandboxClient()

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return getattr(exc, "status", None) == 404

    def _patch_status(self, task: dict[str, Any], phase: CellPhase, **values: Any) -> None:
        allowed = {"jobName", "resolvedSpecDigest", "errorCategory", "resultRef"}
        status = {key: value for key, value in values.items() if key in allowed and value is not None}
        status["phase"] = phase.value
        self.custom.patch_namespaced_custom_object_status(
            GROUP, VERSION, task["metadata"]["namespace"], PLURAL,
            task["metadata"]["name"], {"status": status})

    def _patch_finalizers(self, task: dict[str, Any], finalizers: list[str]) -> None:
        self.custom.patch_namespaced_custom_object(
            GROUP, VERSION, task["metadata"]["namespace"], PLURAL,
            task["metadata"]["name"], {"metadata": {"finalizers": finalizers}})

    def _get(self, reader: Callable[..., Any], name: str, namespace: str) -> Any | None:
        try:
            return reader(name, namespace)
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise

    @staticmethod
    def _owner_uid(value: Any) -> str | None:
        metadata = value.get("metadata", {}) if isinstance(value, dict) else getattr(value, "metadata", None)
        owners = metadata.get("ownerReferences", []) if isinstance(metadata, dict) else getattr(metadata, "owner_references", []) or []
        for owner in owners:
            if (owner.get("controller") if isinstance(owner, dict) else owner.controller):
                return owner.get("uid") if isinstance(owner, dict) else owner.uid
        return None

    def _ensure(self, reader: Callable[..., Any], creator: Callable[..., Any],
                manifest: dict[str, Any]) -> Any:
        name, namespace = manifest["metadata"]["name"], manifest["metadata"]["namespace"]
        existing = self._get(reader, name, namespace)
        expected = manifest["metadata"]["ownerReferences"][0]["uid"]
        if existing is not None:
            if self._owner_uid(existing) != expected:
                raise RuntimeError(f"refusing to adopt {namespace}/{name}")
            return existing
        try:
            return creator(namespace, manifest)
        except Exception as exc:
            if getattr(exc, "status", None) != 409:
                raise
            existing = reader(name, namespace)
            if self._owner_uid(existing) != expected:
                raise RuntimeError(f"refusing to adopt {namespace}/{name}") from exc
            return existing

    @staticmethod
    def _job_phase(job: Any) -> CellPhase:
        status = getattr(job, "status", None)
        if isinstance(job, dict):
            status = job.get("status") or {}
            get = status.get
        else:
            get = lambda key, default=0: getattr(status, key, default)
        if get("succeeded", 0):
            return CellPhase.SUCCEEDED
        if get("failed", 0):
            return CellPhase.FAILED
        return CellPhase.RUNNING

    def _delete_job(self, task: dict[str, Any]) -> bool:
        name = f"{task['metadata']['name']}-worker"
        namespace = task["metadata"]["namespace"]
        try:
            self.batch.delete_namespaced_job(name, namespace, propagation_policy="Foreground")
        except Exception as exc:
            if not self._not_found(exc):
                raise
        return self._get(self.batch.read_namespaced_job, name, namespace) is None

    def _delete_bridge_service(self, task: dict[str, Any]) -> bool:
        name = f"{task['metadata']['name']}-bridge"
        namespace = task["metadata"]["namespace"]
        try:
            self.core.delete_namespaced_service(name, namespace)
        except Exception as exc:
            if not self._not_found(exc):
                raise
        return self._get(self.core.read_namespaced_service, name, namespace) is None

    def _cleanup(self, task: dict[str, Any]) -> bool:
        job_gone = self._delete_job(task)
        service_gone = self._delete_bridge_service(task)
        self.cube.kill_owned_sandboxes(task["metadata"]["uid"])
        return job_gone and service_gone and not self.cube.list_owned_sandboxes(task["metadata"]["uid"])

    def _bridge_endpoint(self, task: dict[str, Any]) -> tuple[str, int, int | None]:
        name = f"{task['metadata']['name']}-bridge"
        namespace = task["metadata"]["namespace"]
        service = self._get(self.core.read_namespaced_service, name, namespace)
        if service is None:
            raise RuntimeError(f"bridge Service {namespace}/{name} is missing")
        spec = service.get("spec", {}) if isinstance(service, dict) else service.spec
        ports = spec.get("ports", []) if isinstance(spec, dict) else spec.ports
        by_name = {
            (item.get("name") if isinstance(item, dict) else item.name): item
            for item in ports
        }
        bridge_port_item = by_name.get("worker-bridge") or ports[0]
        gateway_port_item = by_name.get("model-gateway")
        bridge_port = (bridge_port_item.get("nodePort") if isinstance(bridge_port_item, dict)
                       else bridge_port_item.node_port)
        gateway_port = (gateway_port_item.get("nodePort") if isinstance(gateway_port_item, dict)
                        else gateway_port_item.node_port) if gateway_port_item is not None else None
        node_name = task["spec"]["experimentSpec"]["resources"]["target_node"]
        node = self.core.read_node(node_name)
        status = node.get("status", {}) if isinstance(node, dict) else node.status
        addresses = status.get("addresses", []) if isinstance(status, dict) else status.addresses
        internal = next(
            (item.get("address") if isinstance(item, dict) else item.address
             for item in addresses
             if (item.get("type") if isinstance(item, dict) else item.type) == "InternalIP"),
            None,
        )
        if not internal:
            raise RuntimeError(f"target Worker node {node_name} has no InternalIP")
        openclaw = task["spec"]["experimentSpec"].get("agent", {}).get("driver") == "openclaw"
        if openclaw and not gateway_port:
            raise RuntimeError(f"bridge Service {namespace}/{name} has no model-gateway NodePort")
        return str(internal), int(bridge_port), int(gateway_port) if gateway_port else None

    def _another_worker_active(self, task: dict[str, Any]) -> bool:
        response = self.batch.list_namespaced_job(
            task["metadata"]["namespace"], label_selector="app.kubernetes.io/name=clawbox-experiment-worker")
        items = response.get("items", []) if isinstance(response, dict) else response.items
        own = f"{task['metadata']['name']}-worker"
        for item in items:
            name = item.get("metadata", {}).get("name") if isinstance(item, dict) else item.metadata.name
            if name != own and self._job_phase(item) is CellPhase.RUNNING:
                return True
        return False

    def reconcile(self, task: dict[str, Any], *, allow_workload_start: bool = True) -> bool:
        metadata = task["metadata"]
        finalizers = list(metadata.get("finalizers") or [])
        if metadata.get("deletionTimestamp"):
            if self._cleanup(task) and FINALIZER in finalizers:
                self._patch_finalizers(task, [item for item in finalizers if item != FINALIZER])
            return False
        if FINALIZER not in finalizers:
            self._patch_finalizers(task, finalizers + [FINALIZER])
            return False
        try:
            validate_task(task)
        except (ValueError, TypeError) as exc:
            self._patch_status(task, CellPhase.FAILED, outcome="failed", errorCategory="invalid_spec", message=str(exc))
            return False

        phase = CellPhase((task.get("status") or {}).get("phase", CellPhase.QUEUED.value))
        if task["spec"].get("desiredState", "Running") == "Cancelled" and phase not in TERMINAL:
            if self._cleanup(task):
                self._patch_status(task, CellPhase.CLEANED, outcome="cancelled",
                                   errorCategory="cancelled")
            else:
                self._patch_status(task, CellPhase.CANCELLED, outcome="cancelled",
                                   errorCategory="cleanup_in_progress")
            return False
        if phase in TERMINAL:
            return False

        name, namespace = metadata["name"], metadata["namespace"]
        job = self._get(self.batch.read_namespaced_job, f"{name}-worker", namespace)
        if job is None:
            if not allow_workload_start or self._another_worker_active(task):
                self._patch_status(task, CellPhase.QUEUED, reason="WaitingForWorkerSlot",
                                   resolvedSpecDigest=resolved_spec_digest(task))
                return False
            self._ensure(self.core.read_namespaced_config_map,
                         self.core.create_namespaced_config_map, experiment_configmap(task))
            bridge = self._ensure(self.core.read_namespaced_service,
                                  self.core.create_namespaced_service, worker_bridge_service(task))
            bridge_host, bridge_port, gateway_port = self._bridge_endpoint(task)
            self._ensure(self.batch.read_namespaced_job,
                         self.batch.create_namespaced_job,
                         experiment_worker_job(task, bridge_host=bridge_host,
                                              bridge_node_port=int(bridge_port),
                                              model_gateway_node_port=(
                                                  int(gateway_port) if gateway_port else None
                                              )))
            self._patch_status(task, CellPhase.RUNNING, jobName=f"{name}-worker",
                               resolvedSpecDigest=resolved_spec_digest(task), startedAt=now())
            return True
        job_phase = self._job_phase(job)
        if job_phase is CellPhase.SUCCEEDED:
            self.cube.kill_owned_sandboxes(metadata["uid"])
            service_gone = self._delete_bridge_service(task)
            if self.cube.list_owned_sandboxes(metadata["uid"]):
                self._patch_status(task, CellPhase.FAILED, outcome="failed",
                                   errorCategory="sandbox_cleanup")
            elif not service_gone:
                self._patch_status(task, CellPhase.FAILED, outcome="failed",
                                   errorCategory="bridge_service_cleanup")
            else:
                self._patch_status(task, CellPhase.SUCCEEDED, outcome="succeeded",
                                   resultRef=(f"{task['spec']['resultHostPath'].rstrip('/')}"
                                              f"/{task['spec']['runRef']['runID']}/summary.json"),
                                   completedAt=now())
        elif job_phase is CellPhase.FAILED:
            self.cube.kill_owned_sandboxes(metadata["uid"])
            self._delete_bridge_service(task)
            self._patch_status(task, CellPhase.FAILED, outcome="failed", errorCategory="worker_job")
        else:
            self._patch_status(task, CellPhase.RUNNING, jobName=f"{name}-worker")
        return False
