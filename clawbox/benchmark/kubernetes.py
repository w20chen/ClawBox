from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawbox.controller.kubernetes_backend import dns_label


@dataclass(frozen=True)
class BenchmarkTask:
    instance_id: str
    image: str
    problem_statement: str
    base_commit: str = ""
    hint_text: str = ""


def load_tasks(path: Path) -> list[BenchmarkTask]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else raw.get("tasks", raw.get("instances", []))
    tasks: list[BenchmarkTask] = []
    for record in records:
        instance_id = str(record.get("instance_id") or record.get("task_id") or record.get("id") or "").strip()
        image = str(record.get("image") or record.get("docker_image") or record.get("image_name") or "").strip()
        if record.get("image_tag") and image and ":" not in image.rsplit("/", 1)[-1]:
            image = f"{image}:{record['image_tag']}"
        problem = str(record.get("problem_statement") or record.get("problem") or record.get("prompt") or "")
        if not instance_id or not image:
            raise ValueError("every task requires instance_id/task_id and image/docker_image")
        tasks.append(BenchmarkTask(instance_id, image, problem, str(record.get("base_commit") or ""), str(record.get("hint_text") or "")))
    return tasks


def render_job(
    task: BenchmarkTask,
    *,
    namespace: str,
    bundle_image: str,
    llm_secret: str,
    runtime_class: str = "kata-fc",
    cpu: str = "4",
    memory: str = "8Gi",
    timeout_seconds: int = 1800,
    trace_pvc: str | None = None,
    run_id: str = "run",
) -> dict[str, Any]:
    name = dns_label(f"{run_id}-{task.instance_id}", prefix="swe-")
    tenant = dns_label(task.instance_id)
    labels = {
        "app.kubernetes.io/name": "clawbox-swe-rebench",
        "app.kubernetes.io/managed-by": "clawbox",
        "clawbox.openai.com/tenant": tenant,
        "clawbox.openai.com/task": tenant,
    }
    env = [
        {"name": "TENANT_ID", "value": task.instance_id},
        {"name": "CLAW_RUNTIME_ID", "value": f"swe-rebench-{task.instance_id}"},
        {"name": "TASK_INSTANCE_ID", "value": task.instance_id},
        {"name": "TASK_IMAGE", "value": task.image},
        {"name": "TASK_BASE_COMMIT", "value": task.base_commit},
        {"name": "TASK_HINT_TEXT", "value": task.hint_text},
        {"name": "PROBLEM_STATEMENT", "value": task.problem_statement},
        {"name": "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED", "value": "false"},
    ]
    for variable, key in (
        ("LLM_API_KEY", "llm-api-key"),
        ("LLM_UPSTREAM_BASE_URL", "llm-upstream-base-url"),
        ("LLM_MODEL", "llm-model"),
        ("OPENCLAW_MODEL_REF", "openclaw-model-ref"),
    ):
        env.append({"name": variable, "valueFrom": {"secretKeyRef": {"name": llm_secret, "key": key}}})
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "runtimeClassName": runtime_class,
                    "restartPolicy": "Never",
                    "initContainers": [{
                        "name": "clawtune-bundle", "image": bundle_image,
                        "command": ["/bin/sh", "-ec", "cp -a /bundle/. /claw/" + (" && mkdir -p /trace-root/$TASK_INSTANCE_ID" if trace_pvc else "")],
                        "env": [{"name": "TASK_INSTANCE_ID", "value": task.instance_id}],
                        "volumeMounts": ([{"name": "claw", "mountPath": "/claw"}] + ([{"name": "traces", "mountPath": "/trace-root"}] if trace_pvc else [])),
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    }],
                    "containers": [{
                        "name": "openclaw-runtime", "image": task.image,
                        "command": ["/claw/entrypoint.sh"], "env": env,
                        "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"cpu": cpu, "memory": memory}},
                        "volumeMounts": [
                            {"name": "claw", "mountPath": "/claw", "readOnly": True},
                            {"name": "traces", "mountPath": "/traces", **({"subPathExpr": "$(TASK_INSTANCE_ID)"} if trace_pvc else {})},
                        ],
                    }],
                    "volumes": [
                        {"name": "claw", "emptyDir": {}},
                        {"name": "traces", **({"persistentVolumeClaim": {"claimName": trace_pvc}} if trace_pvc else {"emptyDir": {}})},
                    ],
                },
            },
        },
    }


class KubernetesBenchmarkLauncher:
    def __init__(self, core=None, batch=None):
        if core is None or batch is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            core, batch = client.CoreV1Api(), client.BatchV1Api()
        self.core, self.batch = core, batch

    def run(self, tasks: list[BenchmarkTask], *, parallelism: int, **job_options: Any) -> list[dict[str, Any]]:
        job_options.setdefault("run_id", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = {pool.submit(self._run_one, task, **job_options): task for task in tasks}
            return [future.result() for future in as_completed(futures)]

    def _run_one(self, task: BenchmarkTask, **job_options: Any) -> dict[str, Any]:
        namespace = job_options["namespace"]
        manifest = render_job(task, **job_options)
        name = manifest["metadata"]["name"]
        self.batch.create_namespaced_job(namespace, manifest)
        while True:
            job = self.batch.read_namespaced_job_status(name, namespace)
            status = job.status
            if getattr(status, "succeeded", 0):
                return {"task_id": task.instance_id, "job": name, "status": "succeeded"}
            if getattr(status, "failed", 0):
                return {"task_id": task.instance_id, "job": name, "status": "failed"}
            time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SWE-Rebench task images as concurrent Kata/Firecracker Jobs")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--namespace", default="clawbox-benchmarks")
    parser.add_argument("--bundle-image", required=True)
    parser.add_argument("--llm-secret", default="clawbox-llm")
    parser.add_argument("--runtime-class", default="kata-fc")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--cpu", default="4")
    parser.add_argument("--memory", default="8Gi")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--trace-pvc", help="Existing RWX PVC used for per-task trace directories")
    args = parser.parse_args()
    if args.parallelism < 1:
        parser.error("--parallelism must be >= 1")
    tasks = load_tasks(args.tasks)
    if args.sample is not None:
        tasks = tasks[:args.sample]
    results = KubernetesBenchmarkLauncher().run(
        tasks, parallelism=args.parallelism, namespace=args.namespace,
        bundle_image=args.bundle_image, llm_secret=args.llm_secret,
        runtime_class=args.runtime_class, cpu=args.cpu, memory=args.memory,
        timeout_seconds=args.timeout_seconds,
        trace_pvc=args.trace_pvc,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if any(item["status"] != "succeeded" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
