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
    # QEMU is the most broadly supported Kata backend on arm64/openEuler.
    # Deployments can opt into Firecracker after the host gate has proved a
    # working kata-fc handler and matching arm64 guest assets.
    kubernetes_runtime_class: str = os.getenv("KUBERNETES_RUNTIME_CLASS", "kata-qemu")
    kubernetes_image_pull_policy: str = os.getenv("KUBERNETES_IMAGE_PULL_POLICY", "IfNotPresent")
    kubernetes_ready_timeout_seconds: int = int(os.getenv("KUBERNETES_READY_TIMEOUT_SECONDS", "180"))
    tool_image: str = os.getenv("TOOL_IMAGE", "clawbox-tool-agent:latest")
    lease_ttl_seconds: int = int(os.getenv("LEASE_TTL_SECONDS", "300"))
    reserved_cpu_fraction: float = float(os.getenv("RESERVED_CPU_FRACTION", "0.05"))
    default_memory_bytes: int = int(os.getenv("DEFAULT_MEMORY_BYTES", str(512 * 1024**2)))


settings = Settings()
