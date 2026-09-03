from __future__ import annotations

from types import SimpleNamespace

from clawbox.cell.controller import FINALIZER, CellReconciler


class NotFound(Exception):
    status = 404


class Core:
    def __init__(self) -> None:
        self.maps = {}

    def read_namespaced_config_map(self, name, namespace):
        try: return self.maps[(namespace, name)]
        except KeyError: raise NotFound()

    def create_namespaced_config_map(self, namespace, manifest):
        self.maps[(namespace, manifest["metadata"]["name"])] = manifest
        return manifest


class Batch:
    def __init__(self) -> None:
        self.jobs = {}

    def read_namespaced_job(self, name, namespace):
        try: return self.jobs[(namespace, name)]
        except KeyError: raise NotFound()

    def create_namespaced_job(self, namespace, manifest):
        self.jobs[(namespace, manifest["metadata"]["name"])] = manifest
        return manifest

    def list_namespaced_job(self, namespace, label_selector=None):
        return {"items": [item for (ns, _), item in self.jobs.items() if ns == namespace]}

    def delete_namespaced_job(self, name, namespace, **kwargs):
        if self.jobs.pop((namespace, name), None) is None:
            raise NotFound()


class Custom:
    def __init__(self) -> None:
        self.finalizers = []
        self.statuses = []

    def patch_namespaced_custom_object(self, *args):
        self.finalizers.append(args[-1]["metadata"]["finalizers"])

    def patch_namespaced_custom_object_status(self, *args):
        self.statuses.append(args[-1]["status"])


class Cube:
    journal = None

    def __init__(self) -> None:
        self.cleaned = []

    def kill_owned_sandboxes(self, uid): self.cleaned.append(uid)
    def list_owned_sandboxes(self, uid): return []


def task() -> dict:
    experiment = {
        "schema_version": 2, "experiment_id": "vertical",
        "workload": {"source": "recorded_trace", "input": "/input/trace.jsonl"},
        "agent": {"driver": "replay_engine"}, "inference": {"backend": "replay"},
        "runtime": {"template_alias": "runtime-arm64", "memory_mib": 2048},
        "sandbox": {"template_alias": "arm64"},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 1000,
                      "emergency_free_memory_mib": 100},
        "policies": [{"name": "resident", "admission": "lifetime_full",
                      "reclamation": "resident", "eviction": "none", "restore": "none"}],
    }
    return {
        "apiVersion": "clawbox.openai.com/v1alpha2", "kind": "SandboxTask",
        "metadata": {"name": "run-a", "namespace": "bench", "uid": "uid-a",
                     "generation": 1, "finalizers": []},
        "spec": {"runRef": {"tenantID": "t", "runID": "r", "attemptID": "a"},
                 "workerImage": "worker@sha256:" + "a" * 64,
                 "cubeApiURL": "http://cube-api:3000", "resultHostPath": "/data/results/r/a",
                 "experimentSpec": experiment, "desiredState": "Running"},
    }


def test_controller_creates_exactly_one_node_pinned_worker_job() -> None:
    core, batch, custom, cube = Core(), Batch(), Custom(), Cube()
    reconciler = CellReconciler(core_api=core, batch_api=batch, custom_api=custom,
                                cube_client=cube)
    value = task()
    assert not reconciler.reconcile(value)
    value["metadata"]["finalizers"] = [FINALIZER]
    assert reconciler.reconcile(value)
    assert not reconciler.reconcile(value)
    job = batch.jobs[("bench", "run-a-worker")]
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0
    assert pod["restartPolicy"] == "Never" and pod["nodeName"] == "node-a"
    prepare = pod["initContainers"][0]
    assert prepare["name"] == "prepare-results"
    assert prepare["securityContext"]["capabilities"]["add"] == ["CHOWN", "FOWNER"]
    worker_node = next(item for item in pod["containers"][0]["env"]
                       if item["name"] == "CLAWBOX_WORKER_NODE")
    assert worker_node["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    bridge_host = next(item for item in pod["containers"][0]["env"]
                       if item["name"] == "CLAWBOX_BRIDGE_HOST")
    assert bridge_host["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
    assert custom.statuses[-1]["phase"] == "running"
    assert set(custom.statuses[-1]) <= {"phase", "jobName", "resolvedSpecDigest",
                                        "errorCategory", "resultRef"}


def test_controller_projects_optional_model_credentials_only_into_worker() -> None:
    value = task()
    value["spec"]["credentialSecretName"] = "openclaw-model-credentials"
    value["metadata"]["finalizers"] = [FINALIZER]
    batch = Batch()
    CellReconciler(core_api=Core(), batch_api=batch, custom_api=Custom(),
                   cube_client=Cube()).reconcile(value)
    pod = batch.jobs[("bench", "run-a-worker")]["spec"]["template"]["spec"]
    assert pod["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "openclaw-model-credentials"}}
    ]
    assert "envFrom" not in pod["initContainers"][0]


def test_controller_propagates_success_and_cleans_orphans() -> None:
    core, batch, custom, cube = Core(), Batch(), Custom(), Cube()
    reconciler = CellReconciler(core_api=core, batch_api=batch, custom_api=custom,
                                cube_client=cube)
    value = task()
    value["metadata"]["finalizers"] = [FINALIZER]
    reconciler.reconcile(value)
    batch.jobs[("bench", "run-a-worker")]["status"] = {"succeeded": 1}
    reconciler.reconcile(value)
    assert custom.statuses[-1] == {
        "phase": "succeeded", "resultRef": "/data/results/r/a/r/summary.json",
    }
    assert cube.cleaned == ["uid-a"]
