"""Convenience recipes; policy names never select execution behavior."""
from dataclasses import dataclass
from types import MappingProxyType
from .spec import PolicySpec
from .spec_types import AdmissionPolicy, RuntimeControl, ReclamationPolicy, EvictionPolicy, RestorePolicy

@dataclass(frozen=True)
class Baseline:
    name: str
    admission_policy: AdmissionPolicy
    reclamation_policy: ReclamationPolicy = ReclamationPolicy.RESIDENT
    eviction_policy: EvictionPolicy = EvictionPolicy.NONE
    restore_policy: RestorePolicy = RestorePolicy.NONE
    runtime_control: RuntimeControl = RuntimeControl.AGENTCGROUP_CGROUP
    deployment: str = "managed"

    @property
    def implementation_status(self):
        return 'implemented'

    def as_policy(self, *, name=None):
        return PolicySpec(name=name or self.name, admission=self.admission_policy,
                          runtime_control=self.runtime_control, reclamation=self.reclamation_policy,
                          eviction=self.eviction_policy, restore=self.restore_policy,
                          deployment=self.deployment)

BASELINES = MappingProxyType({b.name: b for b in (
    Baseline('no_control', AdmissionPolicy.OFF, runtime_control=RuntimeControl.NO_CONTROL),
    Baseline('agentcgroup_legacy', AdmissionPolicy.OFF, runtime_control=RuntimeControl.AGENTCGROUP_LEGACY),
    Baseline('phase_memcg', AdmissionPolicy.OFF, runtime_control=RuntimeControl.PHASE_MEMCG),
    Baseline('release_cpu', AdmissionPolicy.OFF, runtime_control=RuntimeControl.RELEASE_CPU),
    Baseline('pressure_lifecycle', AdmissionPolicy.OFF,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE,
             RestorePolicy.REACTIVE, RuntimeControl.PRESSURE_LIFECYCLE),
    Baseline('adaptive_joint', AdmissionPolicy.OFF,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE,
             RestorePolicy.REACTIVE, RuntimeControl.ADAPTIVE_JOINT),
    Baseline('default-resident', AdmissionPolicy.OFF, runtime_control=RuntimeControl.OFF,
             deployment='os-default'),
    Baseline('uncontrolled-resident', AdmissionPolicy.OFF, runtime_control=RuntimeControl.OFF),
    Baseline('runtime-resident', AdmissionPolicy.OFF),
    Baseline('fixed-resident', AdmissionPolicy.FIXED),
    Baseline('predicted-resident', AdmissionPolicy.PREDICTED),
    Baseline('prediction-only-resident', AdmissionPolicy.PREDICTED, runtime_control=RuntimeControl.OFF),
    Baseline('predicted-wait-snapshot', AdmissionPolicy.PREDICTED,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE, RestorePolicy.REACTIVE),
)})
ACTIVE_BASELINES = tuple(BASELINES)

def resolve_baseline(name):
    try:
        return BASELINES[name]
    except KeyError as exc:
        raise ValueError(f'unknown preset {name!r}; choose from {", ".join(BASELINES)}') from exc

def ensure_supported_experiment(spec):
    """Validate mechanisms independently of user-chosen labels."""
    for p in spec.policies:
        if p.eviction not in {EvictionPolicy.NONE, EvictionPolicy.WAIT_AWARE_PRESSURE}:
            raise ValueError('supported eviction policies: none, wait_aware_pressure')
        if p.restore not in {RestorePolicy.NONE, RestorePolicy.REACTIVE}:
            raise ValueError('supported restore policies: none, reactive')
