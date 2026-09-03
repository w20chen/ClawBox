from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import yaml


def owner_reference(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task["metadata"]
    return {"apiVersion": task["apiVersion"], "kind": task["kind"],
            "name": metadata["name"], "uid": metadata["uid"],
            "controller": True, "blockOwnerDeletion": True}


def worker_bridge_service(task: dict[str, Any]) -> dict[str, Any]:
    """Node-routed fixed-port adapter for exactly one SandboxTask Worker."""
    metadata = task["metadata"]
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {
            "name": f"{metadata['name']}-bridge",
            "namespace": metadata["namespace"],
            "labels": {"app.kubernetes.io/name": "clawbox-worker-bridge",
                        "clawbox.openai.com/task-uid": metadata["uid"]},
            "ownerReferences": [owner_reference(task)],
        },
        "spec": {
            "type": "NodePort", "externalTrafficPolicy": "Local",
            "selector": {"clawbox.openai.com/task-uid": metadata["uid"]},
            "ports": [{"name": "worker-bridge", "port": 18080,
                       "targetPort": 18080, "protocol": "TCP"}],
        },
    }


def experiment_configmap(task: dict[str, Any]) -> dict[str, Any]:
    name = task["metadata"]["name"]
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{name}-experiment", "namespace": task["metadata"]["namespace"],
                     "ownerReferences": [owner_reference(task)]},
        "data": {"experiment.yaml": yaml.safe_dump(task["spec"]["experimentSpec"], sort_keys=True)},
    }


def experiment_worker_job(task: dict[str, Any], *, bridge_host: str = "",
                          bridge_node_port: int | None = None) -> dict[str, Any]:
    metadata = task["metadata"]
    spec = task["spec"]
    experiment = spec["experimentSpec"]
    name = metadata["name"]
    run_ref = spec["runRef"]
    target_node = experiment["resources"]["target_node"]
    output = experiment.get("output", {}).get("directory", "/results")
    labels = {"app.kubernetes.io/name": "clawbox-experiment-worker",
              "clawbox.openai.com/task": name,
              "clawbox.openai.com/task-uid": metadata["uid"]}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"{name}-worker", "namespace": metadata["namespace"],
                     "labels": labels, "ownerReferences": [owner_reference(task)]},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": int(spec.get("timeoutSeconds", 21600)),
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never", "serviceAccountName": "clawbox-experiment-worker",
                    "automountServiceAccountToken": False,
                    "nodeName": target_node,
                    "securityContext": {"fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch"},
                    # kubelet does not reliably apply fsGroup ownership to hostPath
                    # volumes.  Prepare only this task's dedicated result directory
                    # before starting the non-root worker.
                    "initContainers": [{
                        "name": "prepare-results", "image": spec["workerImage"],
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c", "chown 10001:10001 /results && chmod 0770 /results"],
                        "securityContext": {
                            "runAsUser": 0, "runAsGroup": 0,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER"]},
                        },
                        "volumeMounts": [{"name": "results", "mountPath": "/results"}],
                    }],
                    "containers": [{
                        "name": "worker", "image": spec["workerImage"],
                        "imagePullPolicy": "IfNotPresent",
                        "args": ["--spec", "/config/experiment.yaml"],
                        "env": [
                            {"name": "CLAWBOX_RUN_ID", "value": run_ref["runID"]},
                            {"name": "CLAWBOX_ATTEMPT_ID", "value": run_ref["attemptID"]},
                            {"name": "CLAWBOX_TASK_UID", "value": metadata["uid"]},
                            {"name": "CLAWBOX_WORKER_NODE", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                            {"name": "CLAWBOX_BRIDGE_HOST", "value": bridge_host},
                            {"name": "CLAWBOX_BRIDGE_NODE_PORT", "value": str(bridge_node_port or "")},
                            {"name": "CUBE_API_URL", "value": spec["cubeApiURL"]},
                            {"name": "CLAWBOX_KUBERNETES_VERSION", "value": os.environ.get("CLAWBOX_KUBERNETES_VERSION", "unknown")},
                            {"name": "CLAWBOX_CONTAINERD_VERSION", "value": os.environ.get("CLAWBOX_CONTAINERD_VERSION", "unknown")},
                            {"name": "CLAWBOX_REVISION", "value": os.environ.get("CLAWBOX_REVISION", "unknown")},
                        ],
                        "envFrom": ([{"secretRef": {"name": spec["credentialSecretName"]}}]
                                    if spec.get("credentialSecretName") else []),
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/config", "readOnly": True},
                            {"name": "results", "mountPath": output},
                            {"name": "host-proc", "mountPath": "/host/proc", "readOnly": True},
                            {"name": "host-cgroup", "mountPath": "/host/sys/fs/cgroup", "readOnly": True},
                            {"name": "cubelet", "mountPath": "/data/cubelet", "readOnly": True},
                            {"name": "inputs", "mountPath": "/data/clawbox-inputs", "readOnly": True},
                        ],
                    }],
                    "volumes": [
                        {"name": "config", "configMap": {"name": f"{name}-experiment"}},
                        {"name": "results", "hostPath": {"path": spec["resultHostPath"], "type": "DirectoryOrCreate"}},
                        {"name": "host-proc", "hostPath": {"path": "/proc", "type": "Directory"}},
                        {"name": "host-cgroup", "hostPath": {"path": "/sys/fs/cgroup", "type": "Directory"}},
                        {"name": "cubelet", "hostPath": {"path": "/data/cubelet", "type": "Directory"}},
                        {"name": "inputs", "hostPath": {"path": "/data/clawbox-inputs", "type": "DirectoryOrCreate"}},
                    ],
                },
            },
        },
    }


def resolved_spec_digest(task: dict[str, Any]) -> str:
    encoded = json.dumps(task["spec"]["experimentSpec"], sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
