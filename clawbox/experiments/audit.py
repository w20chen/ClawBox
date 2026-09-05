"""Read-only audit of checked-in schema-v2 experiment matrices."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .baselines import BASELINES
from .spec import (
    ExperimentSpec,
    expand_matrix,
    load_experiment,
    load_workload_cases,
    spec_digest,
)
from .spec_types import AgentDriver


# These are deliberate gates, not assumptions about the live host. The small
# c1/c4 fixtures remain useful pre-scale checks, while the paper matrices must
# include c20/c40/c60 planning arms.
EXPECTED_CONCURRENCY_LEVELS = {
    "decision.yaml": (20, 40, 60),
    "full-system.yaml": (20, 40, 60),
    "reclamation.yaml": (20, 40, 60),
    "spatial.yaml": (20, 40, 60),
    "openclaw-cube-replay-c40.yaml": (40,),
    "smoke-matrix.yaml": (4,),
    "vertical-slice.yaml": (1,),
    "openclaw-cube.yaml": (1,),
}


def _policy_key(policy: object) -> tuple[object, ...]:
    return (
        policy.admission, policy.reclamation, policy.eviction, policy.restore,
        policy.fixed_delay_seconds, policy.prefetch_lead_seconds,
    )


def _catalog_keys() -> set[tuple[object, ...]]:
    return {_policy_key(baseline.as_policy()) for baseline in BASELINES.values()}


def _artifact_provenance(spec: ExperimentSpec, path: Path) -> dict[str, dict[str, str]]:
    """Return immutable Runtime/Tool provenance required by OpenClaw runs.

    Replay-engine capacity matrices intentionally retain aliases because they
    are historical systems fixtures.  OpenClaw runs, however, are formal
    agent evidence and must identify the exact template record and image that
    the Worker will validate before creating either VM.
    """
    if spec.agent.driver is not AgentDriver.OPENCLAW:
        return {}
    provenance: dict[str, dict[str, str]] = {}
    for role, sandbox in (("runtime", spec.runtime), ("tool", spec.sandbox)):
        if sandbox.template_id is None:
            raise ValueError(
                f"{path}: OpenClaw {role} template must use immutable template_id"
            )
        missing = [
            name for name, value in (
                ("source_image_reference", sandbox.source_image_reference),
                ("image_digest", sandbox.image_digest),
            ) if not value
        ]
        if missing:
            raise ValueError(
                f"{path}: OpenClaw {role} artifact provenance is incomplete; "
                f"missing {', '.join(missing)}"
            )
        provenance[role] = {
            "template_id": sandbox.template_id,
            "source_image_reference": sandbox.source_image_reference or "",
            "image_digest": sandbox.image_digest or "",
        }
    return provenance


def audit_experiment(path: Path, *, expected_levels: tuple[int, ...] | None = None) -> dict[str, object]:
    """Validate one experiment and return a machine-readable audit record."""
    spec: ExperimentSpec = load_experiment(path)
    if expected_levels is not None and spec.execution.concurrency_levels != expected_levels:
        raise ValueError(
            f"{path}: concurrency levels are {list(spec.execution.concurrency_levels)}, "
            f"expected {list(expected_levels)}"
        )
    if "sandbox-code" in spec.sandbox.template:
        raise ValueError(f"{path}: legacy sandbox-code Tool template is not accepted")
    if spec.agent.driver.value == "openclaw":
        prompts = [case.prompt for case in spec.workload.cases]
        if any("cube_shell" in prompt for prompt in prompts):
            raise ValueError(f"{path}: OpenClaw workload still names removed cube_shell")
    artifact_provenance = _artifact_provenance(spec, path)
    catalog_keys = _catalog_keys()
    missing = [
        policy.name for policy in spec.policies
        if _policy_key(policy) not in catalog_keys
    ]
    if missing:
        raise ValueError(f"{path}: policies missing from the baseline catalog: {missing}")
    arms = expand_matrix(spec)
    case_multiplier = (
        1 if spec.workload.session_assignment.value == "round_robin"
        else len(load_workload_cases(spec.workload))
    )
    expected_arms = (
        case_multiplier
        * spec.workload.repetitions
        * len(spec.execution.concurrency_levels)
        * len(spec.policies)
    )
    if len(arms) != expected_arms:
        raise ValueError(f"{path}: planned {len(arms)} arms, expected {expected_arms}")
    return {
        "file": str(path),
        "experiment_id": spec.experiment_id,
        "spec_digest": spec_digest(spec),
        "concurrency_levels": list(spec.execution.concurrency_levels),
        "policy_names": [policy.name for policy in spec.policies],
        "policy_count": len(spec.policies),
        "arm_count": len(arms),
        "agent_driver": spec.agent.driver.value,
        "inference_backend": spec.inference.backend.value,
        "runtime_template": spec.runtime.template,
        "tool_template": spec.sandbox.template,
        "artifact_provenance": artifact_provenance,
        "session_assignment": spec.workload.session_assignment.value,
        "workload_case_count": len(load_workload_cases(spec.workload)),
    }


def audit_experiments(
    paths: Iterable[Path],
    *,
    expected_levels: dict[str, tuple[int, ...]] | None = None,
) -> list[dict[str, object]]:
    """Audit paths in deterministic order, failing on the first bad matrix."""
    mapping = EXPECTED_CONCURRENCY_LEVELS if expected_levels is None else expected_levels
    return [
        audit_experiment(path, expected_levels=mapping.get(path.name))
        for path in sorted(paths, key=lambda item: str(item))
    ]
