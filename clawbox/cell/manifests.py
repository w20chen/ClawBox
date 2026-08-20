from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from clawbox.cell.capacity import CellSize, ResourceVector
from clawbox.common.config import settings
from clawbox.ingester.auth import create_upload_token


def owner_reference(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task["metadata"]
    return {
        "apiVersion": task["apiVersion"],
        "kind": task["kind"],
        "name": metadata["name"],
        "uid": metadata["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def labels(task: dict[str, Any], component: str) -> dict[str, str]:
    metadata = task["metadata"]
    return {
        "app.kubernetes.io/name": "clawbox-cell",
        "app.kubernetes.io/component": component,
        "app.kubernetes.io/managed-by": "clawbox-cell-controller",
        "clawbox.openai.com/cell": metadata["name"],
        "clawbox.openai.com/task": metadata["name"],
    }


def metadata(task: dict[str, Any], name: str, component: str) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": task["metadata"]["namespace"],
        "labels": labels(task, component),
        "ownerReferences": [owner_reference(task)],
    }


def resources(value: ResourceVector) -> dict[str, dict[str, str]]:
    quantities = {
        "cpu": f"{value.cpu_millis}m",
        "memory": str(value.memory_bytes),
        "ephemeral-storage": str(value.storage_bytes),
    }
    return {"requests": quantities, "limits": quantities.copy()}


@dataclass(frozen=True)
class SSHCredentials:
    client_private: str
    client_public: str
    host_private: str
    host_public: str


def _keypair(comment: str) -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()
    return private, f"{public} {comment}\n"


def generate_ssh_credentials(task_id: str) -> SSHCredentials:
    client_private, client_public = _keypair(f"clawbox-client-{task_id}")
    host_private, host_public = _keypair(f"clawbox-host-{task_id}")
    return SSHCredentials(client_private, client_public, host_private, host_public)


def prompt_configmap(task: dict[str, Any]) -> dict[str, Any]:
    name = task["metadata"]["name"]
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": metadata(task, f"{name}-prompt", "prompt"),
        "data": {
            "problem_statement": str(task["spec"]["problemStatement"]),
            "base_commit": str(task["spec"].get("baseCommit", "")),
            "hint_text": str(task["spec"].get("hintText", "")),
        },
    }


def credential_secrets(task: dict[str, Any], credentials: SSHCredentials, timeout_seconds: int) -> list[dict[str, Any]]:
    name = task["metadata"]["name"]
    upload_token = create_upload_token(
        name, settings.ingest_secret, expires_at=int(time.time()) + timeout_seconds + 900,
    )
    common = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque"}
    string_data = {
        "id_ed25519": credentials.client_private,
        "id_ed25519.pub": credentials.client_public,
        "ssh_host_ed25519_key": credentials.host_private,
        "ssh_host_ed25519_key.pub": credentials.host_public,
        "trace-upload-token": upload_token,
    }
    # P2: KB pull/flush credentials (only when the control plane advertises a
    # KB endpoint).  The ingest secret signs this cell's observations so the
    # projector's HMAC gate accepts them.
    if settings.kb_endpoint:
        string_data["kb-token"] = settings.kb_token or settings.service_token
        string_data["kb-ingest-secret"] = settings.kb_ingest_secret or settings.ingest_secret
    return [{
        **common,
        "metadata": metadata(task, f"{name}-auth", "cell-auth"),
        "stringData": string_data,
    }]


def tool_service(task: dict[str, Any]) -> dict[str, Any]:
    name = task["metadata"]["name"]
    selector = labels(task, "tool")
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": metadata(task, f"{name}-tool", "tool"),
        "spec": {
            "type": "ClusterIP", "selector": selector,
            "ports": [{"name": "ssh", "port": 2222, "targetPort": "ssh", "protocol": "TCP"}],
        },
    }


def tool_pod(task: dict[str, Any], size: CellSize, *, node_name: str | None = None) -> dict[str, Any]:
    name = task["metadata"]["name"]
    spec = task["spec"]
    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "runtimeClassName": settings.kubernetes_runtime_class,
        "restartPolicy": "Never",
        "nodeSelector": {
            "kubernetes.io/arch": "arm64",
            "clawbox.openai.com/firecracker-ready": "true",
        },
        # Kata's guest agent writes Secret volume data dirs with mode 0000
        # (probe-verified: `..data` -> `d---------`), so a non-root uid gets
        # EACCES traversing them. Run as root inside the microVM (guest root
        # != host root; the microVM is the isolation boundary). GIT_CONFIG_*
        # skips git "dubious ownership" errors when task commands run as root
        # over the uid-10001-owned /testbed tree.
        "securityContext": {"runAsUser": 0, "runAsGroup": 0, "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        # Kata on this host has no shared filesystem and cannot share volumes
        # across containers (probes: any init+task pair mounting volumes fails
        # agent create_container with ENOENT). The Tool Pod is a SINGLE
        # container: the bridge binary is baked into the task image and the SSH
        # keys arrive via a Secret volume (single-container mounts work).
        "containers": [{
            "name": "task", "image": spec["toolImage"],
            "imagePullPolicy": settings.kubernetes_image_pull_policy,
            "command": ["/usr/local/bin/tool-bridge"],
            "ports": [{"name": "ssh", "containerPort": 2222, "protocol": "TCP"}],
            "env": [
                {"name": "CELL_ID", "value": name}, {"name": "TASK_ID", "value": name},
                {"name": "TOOL_BRIDGE_WORKDIR", "value": "/testbed"},
                {"name": "TOOL_BRIDGE_LOG_PATH", "value": "/testbed/.clawbox/tool-bridge.jsonl"},
                {"name": "TOOL_EXEC_TIMEOUT_SECONDS", "value": str(spec.get("commandTimeoutSeconds", 300))},
                {"name": "TOOL_OUTPUT_LIMIT_BYTES", "value": str(spec.get("outputLimitBytes", 4 * 1024**2))},
                {"name": "GIT_CONFIG_COUNT", "value": "1"},
                {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
                {"name": "GIT_CONFIG_VALUE_0", "value": "*"},
            ],
            "readinessProbe": {"tcpSocket": {"port": "ssh"}, "periodSeconds": 2, "failureThreshold": 30},
            "resources": resources(size.tool),
            # allowPrivilegeEscalation:false + drop ALL makes Kata's agent fall
            # back to the image USER (10001) and ignore pod runAsUser 0
            # (probe-j vs probe-k). Running as root inside the microVM is the
            # whole point, so no escalation restriction at container level.
            # CAP_SYS_ADMIN lets the tool-bridge remount the guest's
            # read-only cgroup2 tree rw and create per-execution cgroups for
            # exact cpu/io accounting (probe-verified feasible in-guest). The
            # microVM is the isolation boundary; process-tree collection still
            # works without this cap (best-effort fallback in the bridge).
            "securityContext": {"readOnlyRootFilesystem": False,
                                "capabilities": {"add": ["SYS_ADMIN"]}},
            "volumeMounts": [
                {"name": "tool-auth", "mountPath": "/var/run/secrets/tool-ssh", "readOnly": True},
            ],
        }],
        "volumes": [
            {"name": "tool-auth", "secret": {"secretName": f"{name}-auth", "defaultMode": 0o444,
                "items": [
                    {"key": "id_ed25519.pub", "path": "id_ed25519.pub"},
                    {"key": "ssh_host_ed25519_key", "path": "ssh_host_ed25519_key"},
                    {"key": "ssh_host_ed25519_key.pub", "path": "ssh_host_ed25519_key.pub"},
                ]}},
        ],
    }
    if node_name:
        pod_spec["nodeName"] = node_name
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": metadata(task, f"{name}-tool", "tool"),
        "spec": pod_spec,
    }


def _secret_env(secret: str, variable: str, key: str) -> dict[str, Any]:
    return {"name": variable, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def _kb_tenant_id(task: dict[str, Any]) -> str:
    """Return the stable tenant identity without changing per-cell identity.

    v1alpha2 carries the lossless value in spec.runRef.  The live research
    cluster still serves v1alpha1, where the dispatcher-provided tenant label
    is the best available stable key.  Falling back to the task name preserves
    compatibility for hand-authored CRs.
    """
    run_ref = task.get("spec", {}).get("runRef", {})
    return str(
        run_ref.get("tenantID")
        or task.get("metadata", {}).get("labels", {}).get("clawbox.openai.com/tenant")
        or task["metadata"]["name"]
    )


def runtime_job(task: dict[str, Any], size: CellSize, *, node_name: str | None = None) -> dict[str, Any]:
    name = task["metadata"]["name"]
    spec = task["spec"]
    llm_secret = spec["llmSecretName"]
    timeout = int(spec.get("timeoutSeconds", 1800))
    # Leave a pipeline margin after the agent's own budget: the agent runs with
    # --timeout=timeout, then patch collection + result/upload must still finish
    # before the Job's activeDeadlineSeconds. Equal values cut the pipeline at
    # the deadline (observed: RuntimeFailed/DeadlineExceeded right at the agent
    # timeout). 300s proved too tight (2026-08-17: a stalled patch-collection
    # ssh blew the margin and the cell hit CellDeadlineExceeded while the
    # entrypoint was still alive); 600s gives the post-agent phase headroom for
    # two bounded ssh steps (up to ~130s each with timeout -k) plus the final
    # upload. Keep in sync with CellReconciler._timed_out.
    pipeline_grace_seconds = 600
    state_mount = {"name": "state", "mountPath": "/state"}
    llm_env = [
        _secret_env(llm_secret, "OPENAI_API_KEY", "llm-api-key"),
        _secret_env(llm_secret, "OPENAI_BASE_URL", "llm-upstream-base-url"),
        _secret_env(llm_secret, "OPENCLAW_MODEL", "llm-model"),
        _secret_env(llm_secret, "OPENCLAW_MODEL_REF", "openclaw-model-ref"),
    ]
    upload_env = [
        {"name": "TASK_ID", "value": name},
        {"name": "CELL_ID", "value": name},
        {"name": "TRACE_INGESTER_URL", "value": settings.trace_ingester_url},
        _secret_env(f"{name}-auth", "TRACE_UPLOAD_TOKEN", "trace-upload-token"),
        {"name": "CLAWBOX_STATE_DIR", "value": f"/state/{name}"},
        {"name": "CLAWTUNE_TRACE_DIR", "value": f"/state/{name}/traces"},
    ]
    # P2: control-plane KB pull/flush wiring (only when kb_endpoint is set).
    if settings.kb_endpoint:
        upload_env += [
            {"name": "CLAWBOX_KB_ENDPOINT", "value": settings.kb_endpoint},
            _secret_env(f"{name}-auth", "CLAWBOX_KB_TOKEN", "kb-token"),
            _secret_env(f"{name}-auth", "CLAWBOX_KB_INGEST_SECRET", "kb-ingest-secret"),
        ]
    extra_env: list[dict[str, Any]] = []
    if spec.get("repoKey"):
        # Explicit repo namespace from the task spec (v1alpha2 path; on v1alpha1
        # the field is pruned server-side and the runtime derives it from the
        # tool VM's git remote instead).
        extra_env.append({"name": "CLAWTUNE_REPO_KEY", "value": str(spec["repoKey"])})
    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "runtimeClassName": settings.kubernetes_runtime_class,
        "restartPolicy": "Never",
        "nodeSelector": {
            "kubernetes.io/arch": "arm64",
            "clawbox.openai.com/firecracker-ready": "true",
        },
        # Same root-run rationale as the tool pod: Kata's agent writes Secret
        # and ConfigMap volume data dirs with mode 0000, unreadable by uid
        # 10001. Root inside the microVM reads them (and /prompt) fine.
        "securityContext": {"runAsUser": 0, "runAsGroup": 0, "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        # Kata on this host cannot share volumes across containers, so the
        # runtime Job is a SINGLE container (no sidecar init): prompt and SSH
        # keys come straight from ConfigMap/Secret volumes, which work for
        # single-container pods (verified by probes).
        "containers": [{
            "name": "runtime", "image": settings.runtime_image,
            "imagePullPolicy": settings.kubernetes_image_pull_policy,
            "command": ["/usr/local/bin/runtime-entrypoint"],
            "env": llm_env + upload_env + extra_env + [
                {"name": "TENANT_ID", "value": name},
                # TENANT_ID remains the unique cell/state identity.  KB data is
                # shared by the stable managed tenant across successive cells.
                {"name": "CLAWBOX_TENANT_ID", "value": _kb_tenant_id(task)},
                {"name": "RUNTIME_ID", "value": name},
                {"name": "CLAWBOX_TASK_MODE", "value": "benchmark"},
                {"name": "TASK_PROMPT_FILE", "value": "/prompt/problem_statement"},
                {"name": "TASK_TIMEOUT_SECONDS", "value": str(timeout)},
                {"name": "RESOURCE_PROFILE", "value": str(spec.get("profile", "small"))},
                {"name": "TOOL_SSH_TARGET", "value": f"executor@{name}-tool:2222"},
                {"name": "GIT_CONFIG_COUNT", "value": "1"},
                {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
                {"name": "GIT_CONFIG_VALUE_0", "value": "*"},
            ],
            "resources": resources(size.runtime),
            # Same as tool pod: no allowPrivilegeEscalation/drop-ALL so the
            # agent honors pod runAsUser 0 instead of the image USER.
            "securityContext": {"readOnlyRootFilesystem": True},
            "volumeMounts": [
                state_mount,
                {"name": "home", "mountPath": "/home/openclaw"},
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
                {"name": "prompt", "mountPath": "/prompt", "readOnly": True},
                {"name": "auth", "mountPath": "/var/run/secrets/tool-ssh", "readOnly": True},
            ],
        }],
        "volumes": [
            {"name": "state", "emptyDir": {"sizeLimit": "8Gi"}},
            {"name": "home", "emptyDir": {"sizeLimit": "2Gi"}},
            {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
            {"name": "prompt", "configMap": {"name": f"{name}-prompt"}},
            {"name": "auth", "secret": {"secretName": f"{name}-auth", "defaultMode": 0o444,
                "items": [
                    {"key": "id_ed25519", "path": "id_ed25519"},
                    {"key": "ssh_host_ed25519_key.pub", "path": "ssh_host_ed25519_key.pub"},
                ]}},
        ],
    }
    if node_name:
        pod_spec["nodeName"] = node_name
    job_labels = labels(task, "runtime")
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": metadata(task, f"{name}-runtime", "runtime"),
        "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": timeout + pipeline_grace_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": job_labels}, "spec": pod_spec},
        },
    }


def network_policies(task: dict[str, Any]) -> list[dict[str, Any]]:
    name = task["metadata"]["name"]
    cell_selector = {"matchLabels": {"clawbox.openai.com/cell": name}}
    runtime_selector = {"matchLabels": {"clawbox.openai.com/cell": name, "app.kubernetes.io/component": "runtime"}}
    tool_selector = {"matchLabels": {"clawbox.openai.com/cell": name, "app.kubernetes.io/component": "tool"}}
    dns = {
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }
    runtime_egress = [
        dns,
        {"to": [{"podSelector": tool_selector}], "ports": [{"protocol": "TCP", "port": 2222}]},
        {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "clawbox-system"}},
                 "podSelector": {"matchLabels": {"app.kubernetes.io/component": "trace-ingester"}}}],
         "ports": [{"protocol": "TCP", "port": 8084}]},
        {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "clawbox-system"}},
                 "podSelector": {"matchLabels": {"app.kubernetes.io/component": "tune-kb"}}}],
         "ports": [{"protocol": "TCP", "port": 8086}]},
        {"to": [{"ipBlock": {"cidr": task["spec"]["llmEgressCIDR"]}}],
         "ports": [{"protocol": "TCP", "port": int(task["spec"].get("llmEgressPort", 443))}]},
    ]
    tool_egress = [dns]
    for cidr in task["spec"].get("toolEgressCIDRs", []):
        tool_egress.append({"to": [{"ipBlock": {"cidr": cidr}}]})
    policies = [
        ("default-deny", {"podSelector": cell_selector, "policyTypes": ["Ingress", "Egress"]}),
        ("tool-ingress", {"podSelector": tool_selector, "policyTypes": ["Ingress"],
                          "ingress": [{"from": [{"podSelector": runtime_selector}],
                                       "ports": [{"protocol": "TCP", "port": 2222}]}]}),
        ("runtime-egress", {"podSelector": runtime_selector, "policyTypes": ["Egress"], "egress": runtime_egress}),
        ("tool-egress", {"podSelector": tool_selector, "policyTypes": ["Egress"], "egress": tool_egress}),
    ]
    return [{
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": metadata(task, f"{name}-{suffix}", "network"), "spec": policy,
    } for suffix, policy in policies]
