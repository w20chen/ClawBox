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
    runtime_control: RuntimeControl = RuntimeControl.OFF
    deployment: str = "managed"
    comparison_groups: tuple[str, ...] = ()
    idle_release_ranking: bool = False
    runtime_snapshot: bool = True

    @property
    def implementation_status(self):
        return 'implemented'

    def as_policy(self, *, name=None):
        return PolicySpec(name=name or self.name, admission=self.admission_policy,
                          runtime_control=self.runtime_control, reclamation=self.reclamation_policy,
                          eviction=self.eviction_policy, restore=self.restore_policy,
                          deployment=self.deployment,
                          idle_release_ranking=self.idle_release_ranking,
                          runtime_snapshot=self.runtime_snapshot)

BASELINES = MappingProxyType({b.name: b for b in (
    Baseline('phase_memcg', AdmissionPolicy.OFF, runtime_control=RuntimeControl.PHASE_MEMCG,
             comparison_groups=('runtime-control', 'joint-control')),
    Baseline('release_cpu', AdmissionPolicy.OFF, runtime_control=RuntimeControl.RELEASE_CPU,
             comparison_groups=('runtime-control', 'joint-control')),
    Baseline('pressure_lifecycle', AdmissionPolicy.OFF,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE,
             RestorePolicy.REACTIVE, RuntimeControl.PRESSURE_LIFECYCLE,
             comparison_groups=('idle-reclaim', 'joint-control'), runtime_snapshot=False),
    Baseline('adaptive_joint', AdmissionPolicy.OFF,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE,
             RestorePolicy.REACTIVE, RuntimeControl.ADAPTIVE_JOINT,
             comparison_groups=('joint-control',), runtime_snapshot=False),
    Baseline('default-resident', AdmissionPolicy.OFF, runtime_control=RuntimeControl.OFF,
             deployment='os-default', comparison_groups=('deployment',)),
    Baseline('uncontrolled-resident', AdmissionPolicy.OFF, runtime_control=RuntimeControl.OFF,
             comparison_groups=('deployment', 'admission', 'runtime-control',
                                'idle-reclaim', 'joint-control')),
    Baseline('fixed-resident', AdmissionPolicy.FIXED,
             comparison_groups=('admission',)),
    Baseline('predicted-resident', AdmissionPolicy.PREDICTED,
             comparison_groups=('admission', 'integrated')),
    Baseline('predicted-wait-snapshot', AdmissionPolicy.PREDICTED,
             ReclamationPolicy.SNAPSHOT_PAUSE, EvictionPolicy.WAIT_AWARE_PRESSURE,
             RestorePolicy.REACTIVE, comparison_groups=('integrated',),
             idle_release_ranking=True, runtime_snapshot=False),
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
