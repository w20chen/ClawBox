from enum import StrEnum


class WorkloadSource(StrEnum):
    SWE_REBENCH = "swe_rebench"
    RECORDED_TRACE = "recorded_trace"
    SYNTHETIC = "synthetic"


class SessionAssignment(StrEnum):
    SINGLE_CASE = "single_case"
    ROUND_ROBIN = "round_robin"


class ArrivalSchedule(StrEnum):
    BURST = "burst"
    FIXED_STAGGER = "fixed_stagger"


class AgentDriver(StrEnum):
    OPENCLAW = "openclaw"


class InferenceBackend(StrEnum):
    API = "api"
    REPLAY = "replay"


class AdmissionPolicy(StrEnum):
    LIFETIME_FULL = "lifetime_full"
    TOOL_FULL = "tool_full"
    TOOL_STATIC = "tool_static"
    TOOL_P90 = "tool_p90"
    TOOL_ORACLE = "tool_oracle"


class ReclamationPolicy(StrEnum):
    RESIDENT = "resident"
    SNAPSHOT_PAUSE = "snapshot_pause"


class EvictionPolicy(StrEnum):
    NONE = "none"
    EAGER = "eager"
    FIXED_DELAY = "fixed_delay"
    WAIT_AWARE_PRESSURE = "wait_aware_pressure"
    TIME_ORACLE = "time_oracle"
    TIERED_LRU_ORACLE = "tiered_lru_oracle"
    TIERED_TIME_ORACLE = "tiered_time_oracle"


class SnapshotTier(StrEnum):
    LOCAL = "local"
    WARM = "warm"
    COLD = "cold"


class RestorePolicy(StrEnum):
    NONE = "none"
    REACTIVE = "reactive"
    PROACTIVE = "proactive"
