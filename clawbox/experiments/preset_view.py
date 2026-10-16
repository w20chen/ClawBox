"""Select convenience presets using independent mechanism dimensions."""
from .baselines import BASELINES
from .spec_types import RuntimeControl
DIMENSIONS = {'admission': ('off', 'fixed', 'predicted'),
              'runtime_control': tuple(p.value for p in RuntimeControl),
              'reclamation': ('resident', 'snapshot_pause')}

def dimensions(policy):
    return {key: getattr(policy, key).value for key in DIMENSIONS}

def select_presets(names=(), **filters):
    if not any(filters.values()):
        return list(names)
    for key, values in filters.items():
        if key not in DIMENSIONS or any(v not in DIMENSIONS[key] for v in (values or ())):
            raise ValueError(f'unknown preset dimension or value: {key}')
    result = [name for name in names or BASELINES
              if all(not values or dimensions(BASELINES[name].as_policy())[key] in values
                     for key, values in filters.items())]
    if not result:
        raise ValueError('No preset matches; specify independent policy fields in the experiment YAML')
    return result
