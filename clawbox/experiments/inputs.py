"""Read-only input checks shared by the public CLI before starting VMs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .spec import ExperimentSpec, load_workload_cases
from .spec_types import AdmissionPolicy, AgentDriver, InferenceBackend
from clawbox.replay.trace import load_trace


def inspect_trace(path: Path) -> dict:
    actions = load_trace(path)
    ids = [action.action_id for action in actions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: action IDs must be unique")
    return {
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "actions": len(actions),
        "model_calls": sum(action.kind == "llm" for action in actions),
        "tool_calls": sum(action.kind == "tool" for action in actions),
        "model_wait_seconds": sum(action.duration_s for action in actions if action.kind == "llm"),
        "model_requests_present": all(action.input is not None for action in actions if action.kind == "llm"),
        "model_responses_present": all(action.output is not None for action in actions if action.kind == "llm"),
    }


def validate_inputs(spec: ExperimentSpec) -> dict:
    traces = []
    cases = load_workload_cases(spec.workload)
    if not cases:
        raise ValueError("workload contains no cases")
    for case in cases:
        if spec.agent.driver is AgentDriver.OPENCLAW and not case.prompt.strip():
            raise ValueError(f"{case.case_id}: OpenClaw requires a nonempty task prompt")
        if spec.inference.backend is InferenceBackend.REPLAY:
            path = Path(case.replay_trace_reference or case.source_reference)
            info = inspect_trace(path)
            if spec.agent.driver is AgentDriver.OPENCLAW:
                if (not info["model_calls"] or not info["model_requests_present"]
                        or not info["model_responses_present"]):
                    raise ValueError(f"{case.case_id}: managed replay requires recorded model requests and responses")
            traces.append({"case_id": case.case_id, **info})
    files = []
    admissions = {policy.admission for policy in spec.policies}
    for policy, source in (
        (AdmissionPolicy.TOOL_P90, spec.resources.p90_predictions),
        (AdmissionPolicy.TOOL_ORACLE, spec.resources.oracle_measurements),
    ):
        if policy in admissions:
            path = Path(str(source))
            if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                raise ValueError(f"{path}: prediction data must be a JSON object")
            files.append(str(path))
    return {"traces": traces, "prediction_files": files}
