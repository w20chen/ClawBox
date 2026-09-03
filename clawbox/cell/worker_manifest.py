from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml


def owner_reference(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task["metadata"]
    return {"apiVersion": task["apiVersion"], "kind": task["kind"],
            "name": metadata["name"], "uid": metadata["uid"],
            "controller": True, "blockOwnerDeletion": True}


def experiment_configmap(task: dict[str, Any]) -> dict[str, Any]:
    name = task["metadata"]["name"]
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{name}-experiment", "namespace": task["metadata"]["namespace"],
                     "ownerReferences": [owner_reference(task)]},
        "data": {"experiment.yaml": yaml.safe_dump(task["spec"]["experimentSpec"], sort_keys=True)},
    }


def experiment_worker_job(task: dict[str, Any]) -> dict[str, Any]:
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
                    "containers": [{
                        "name": "worker", "image": spec["workerImage"],
                        "imagePullPolicy": "IfNotPresent",
                        "args": ["--spec", "/config/experiment.yaml"],
                        "env": [
                            {"name": "CLAWBOX_RUN_ID", "value": run_ref["runID"]},
                            {"name": "CLAWBOX_ATTEMPT_ID", "value": run_ref["attemptID"]},
                            {"name": "CLAWBOX_TASK_UID", "value": metadata["uid"]},
                            {"name": "CLAWBOX_WORKER_NODE", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                            {"name": "CUBE_API_URL", "value": spec["cubeApiURL"]},
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
