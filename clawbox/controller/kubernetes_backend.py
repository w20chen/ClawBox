from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from clawbox.common.config import settings
from clawbox.common.auth import grant_public_key
from clawbox.common.models import ToolSpec


def dns_label(value: str, *, prefix: str = "", max_length: int = 63) -> str:
    if max_length < len(prefix) + 12 or max_length > 63:
        raise ValueError("max_length must leave room for the prefix and stable digest")
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "tenant"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    head = f"{prefix}{normalized}"[: max_length - len(digest) - 1].rstrip("-")
    return f"{head}-{digest}"


@dataclass(frozen=True)
class KubernetesTool:
    namespace: str
    pod_name: str
    service_name: str
    pod_uid: str
    endpoint: str

    @property
    def backend_id(self) -> str:
        return f"{self.namespace}/{self.pod_name}/{self.service_name}"


class KubernetesBackend:
    def __init__(self, core_api=None):
        if core_api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            core_api = client.CoreV1Api()
        self.core = core_api

    def create(self, spec: ToolSpec, logical_uid: str) -> KubernetesTool:
        namespace = dns_label(spec.tenant_id, prefix=f"{settings.kubernetes_namespace_prefix}-")
        name = dns_label(logical_uid, prefix="tool-")
        self._ensure_namespace(namespace, spec.tenant_id)
        labels = {
            "app.kubernetes.io/name": "clawbox",
            "app.kubernetes.io/component": "tool-agent",
            "app.kubernetes.io/managed-by": "clawbox-controller",
            "clawbox.openai.com/tenant": dns_label(spec.tenant_id),
            "clawbox.openai.com/workspace": dns_label(spec.workspace_id),
        }
        pod = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "automountServiceAccountToken": False,
                "runtimeClassName": settings.kubernetes_runtime_class,
                "restartPolicy": "Always",
                "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "tool-agent", "image": spec.image,
                    "imagePullPolicy": settings.kubernetes_image_pull_policy,
                    "ports": [{"name": "http", "containerPort": 8090}],
                    "env": [
                        {"name": "TOOL_POD_UID", "value": logical_uid},
                        {"name": "TENANT_ID", "value": spec.tenant_id},
                        {"name": "WORKSPACE_ID", "value": spec.workspace_id},
                        {"name": "CLAWBOX_GRANT_PUBLIC_KEY", "value": grant_public_key()},
                    ],
                    "resources": {
                        "requests": {"cpu": str(spec.cpu_count), "memory": str(spec.memory_bytes)},
                        "limits": {"cpu": str(spec.cpu_count), "memory": str(spec.memory_bytes)},
                    },
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 2},
                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                }],
                "volumes": [{"name": "workspace", "emptyDir": {}}],
            },
        }
        service = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {"selector": labels, "ports": [{"name": "http", "port": 8090, "targetPort": "http"}]},
        }
        try:
            created = self.core.create_namespaced_pod(namespace, pod)
            self.core.create_namespaced_service(namespace, service)
            self._wait_ready(namespace, name)
        except Exception:
            self.delete(f"{namespace}/{name}/{name}")
            raise
        pod_uid = str(getattr(getattr(created, "metadata", None), "uid", None) or logical_uid)
        return KubernetesTool(namespace, name, name, pod_uid, f"http://{name}.{namespace}.svc:8090")

    def delete(self, backend_id: str) -> None:
        namespace, pod_name, service_name = backend_id.split("/", 2)
        from kubernetes.client.exceptions import ApiException
        for delete in (
            lambda: self.core.delete_namespaced_service(service_name, namespace),
            lambda: self.core.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=0),
        ):
            try:
                delete()
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def _ensure_namespace(self, namespace: str, tenant_id: str) -> None:
        from kubernetes.client.exceptions import ApiException
        try:
            self.core.read_namespace(namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.core.create_namespace({"metadata": {"name": namespace, "labels": {
                "app.kubernetes.io/managed-by": "clawbox-controller",
                "clawbox.openai.com/tenant": dns_label(tenant_id),
            }}})

    def _wait_ready(self, namespace: str, pod_name: str) -> None:
        deadline = time.monotonic() + settings.kubernetes_ready_timeout_seconds
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(pod_name, namespace)
            statuses = getattr(getattr(pod, "status", None), "container_statuses", None) or []
            if statuses and all(bool(getattr(item, "ready", False)) for item in statuses):
                return
            phase = getattr(getattr(pod, "status", None), "phase", "")
            if phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"Kubernetes tool pod terminated during startup: {phase}")
            time.sleep(1)
        raise TimeoutError(f"Kubernetes tool pod was not ready after {settings.kubernetes_ready_timeout_seconds}s")
