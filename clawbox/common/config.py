from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./clawbox.db")
    service_token: str = os.getenv("CLAWBOX_SERVICE_TOKEN", "development-only-token")
    grant_secret: str = os.getenv("CLAWBOX_GRANT_SECRET", "development-only-grant-secret")
    allocator_url: str = os.getenv("ALLOCATOR_URL", "http://allocator:8081")
    controller_url: str = os.getenv("CONTROLLER_URL", "http://controller:8082")
    scheduler_url: str = os.getenv("SCHEDULER_URL", "http://tenant-scheduler:8080")
    controller_backend: str = os.getenv("CONTROLLER_BACKEND", "docker")
    kubernetes_namespace_prefix: str = os.getenv("KUBERNETES_NAMESPACE_PREFIX", "clawbox-tenant")
    # This project is Firecracker-first.  The bootstrap only creates this
    # RuntimeClass after its handler and all arm64 guest assets pass FC-0.
    kubernetes_runtime_class: str = os.getenv("KUBERNETES_RUNTIME_CLASS", "kata-fc-arm64")
    # Tool commands need the audited debug guest kernel whose BPF/kprobe/perf
    # configuration passed the native collector gate. Runtime/OpenClaw pods do
    # not load BPF and remain on the production handler.
    kubernetes_tool_runtime_class: str = os.getenv(
        "KUBERNETES_TOOL_RUNTIME_CLASS", "kata-fc-arm64-ebpf"
    )
    kubernetes_image_pull_policy: str = os.getenv("KUBERNETES_IMAGE_PULL_POLICY", "IfNotPresent")
    kubernetes_ready_timeout_seconds: int = int(os.getenv("KUBERNETES_READY_TIMEOUT_SECONDS", "180"))
    tool_image: str = os.getenv("TOOL_IMAGE", "clawbox-tool-agent:latest")
    runtime_image: str = os.getenv("RUNTIME_IMAGE", "clawbox-runtime-arm64:latest")
    tool_bridge_image: str = os.getenv("TOOL_BRIDGE_IMAGE", "clawbox-tool-bridge-arm64:latest")
    trace_ingester_url: str = os.getenv("TRACE_INGESTER_URL", "http://clawbox-ingester.clawbox-system.svc:8084")
    ingest_secret: str = os.getenv("CLAWBOX_INGEST_SECRET", "development-only-ingest-secret")
    # P2: control-plane KB endpoint + token + ingest secret the cell controller
    # forwards into each runtime Job so pods pull the (tenant, repo) snapshot at
    # start and flush observations at end.  Empty kb_endpoint disables the loop.
    kb_endpoint: str = os.getenv("CLAWBOX_KB_ENDPOINT", "")
    kb_token: str = os.getenv("CLAWBOX_KB_TOKEN", "")
    kb_ingest_secret: str = os.getenv("CLAWBOX_KB_INGEST_SECRET", "")
    clawtune_revision: str = os.getenv(
        "CLAWTUNE_REVISION", "e91e60bc1e5f3209fbcf6091013fde96f217e2a7"
    )
    cell_namespace: str = os.getenv("CLAWBOX_CELL_NAMESPACE", "clawbox-benchmarks")
    lease_ttl_seconds: int = int(os.getenv("LEASE_TTL_SECONDS", "300"))
    reserved_cpu_fraction: float = float(os.getenv("RESERVED_CPU_FRACTION", "0.05"))
    default_memory_bytes: int = int(os.getenv("DEFAULT_MEMORY_BYTES", str(512 * 1024**2)))


settings = Settings()
