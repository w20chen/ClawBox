"""Shared enums kept separate to avoid a spec/baseline import cycle."""
from enum import StrEnum


class WorkloadSource(StrEnum):
    SWE_REBENCH = "swe_rebench"
    RECORDED_TRACE = "recorded_trace"
    SYNTHETIC = "synthetic"


class AgentDriver(StrEnum):
    OPENCLAW = "openclaw"
    REPLAY_ENGINE = "replay_engine"


class InferenceBackend(StrEnum):
    API = "api"
    REPLAY = "replay"


class SandboxBackend(StrEnum):
    KUBERNETES = "kubernetes"
    DIRECT_FIRECRACKER = "direct_firecracker"
    LOCAL = "local"


class ToolTransport(StrEnum):
    SSH = "ssh"
    VSOCK = "vsock"
    LOCAL = "local"
    KUBECTL = "kubectl"


class AdmissionPolicy(StrEnum):
    FIXED_PROFILE = "fixed_profile"
    FIXED_EXPLICIT = "fixed_explicit"
    P90_STATIC = "p90_static"
    P90_ELASTIC = "p90_elastic"


class ResidencyPolicy(StrEnum):
    RESIDENT = "resident"
    LLM_WAIT_CHECKPOINT = "llm_wait_checkpoint"
    PRESSURE_CHECKPOINT = "pressure_checkpoint"
