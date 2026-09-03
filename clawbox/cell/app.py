from __future__ import annotations

import os
import sys
import time
import traceback

from clawbox.cell.controller import CellReconciler, GROUP, PLURAL, VERSION
from clawbox.common.config import settings


def build_reconciler() -> CellReconciler:
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core = client.CoreV1Api()
    return CellReconciler(
        core_api=core,
        batch_api=client.BatchV1Api(),
        custom_api=client.CustomObjectsApi(),
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
