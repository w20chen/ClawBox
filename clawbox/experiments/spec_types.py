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
    OFF = "off"
    FIXED = "fixed"
    PREDICTED = "predicted"


class RuntimeControl(StrEnum):
    OFF = "off"
    AGENTCGROUP_CGROUP = "agentcgroup_cgroup"
    NO_CONTROL = "no_control"
    AGENTCGROUP_LEGACY = "agentcgroup_legacy"
    PHASE_MEMCG = "phase_memcg"
    RELEASE_CPU = "release_cpu"
    PRESSURE_LIFECYCLE = "pressure_lifecycle"
    ADAPTIVE_JOINT = "adaptive_joint"
    EXPLICIT = "explicit"

    @property
    def legacy(self):
        return self in {self.AGENTCGROUP_CGROUP, self.AGENTCGROUP_LEGACY}

    @property
    def adaptive(self):
        return self in {self.EXPLICIT, self.PHASE_MEMCG, self.RELEASE_CPU,
                        self.PRESSURE_LIFECYCLE, self.ADAPTIVE_JOINT}

    @property
    def coordinates_release(self):
        return self in {self.PRESSURE_LIFECYCLE, self.ADAPTIVE_JOINT}


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
