"""Read-only input checks shared by the public CLI before starting VMs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .spec import ExperimentSpec, load_workload_cases
from .spec_types import AdmissionPolicy, AgentDriver, InferenceBackend
from .prediction import CommandPredictionProvider, P50PredictionProvider
from clawbox.replay.trace import load_trace


def inspect_trace(path: Path) -> dict:
    actions = load_trace(path)
    ids = [action.action_id for action in actions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: action IDs must be unique")
    tool_names = set()
    for action in actions:
        if action.kind != "llm" or not isinstance(action.output, dict):
            continue
        message = action.output
        if isinstance(message.get("content"), dict):
            message = message["content"]
        for call in message.get("tool_calls", []):
            tool_names.add(str((call.get("function") or {}).get("name", "")))
    return {
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "actions": len(actions),
        "model_calls": sum(action.kind == "llm" for action in actions),
        "tool_calls": sum(action.kind == "tool" for action in actions),
        "model_wait_seconds": sum(action.duration_s for action in actions if action.kind == "llm"),
        "model_requests_present": all(action.input is not None for action in actions if action.kind == "llm"),
        "model_responses_present": all(action.output is not None for action in actions if action.kind == "llm"),
        "response_tool_names": sorted(tool_names),
    }


def validate_inputs(spec: ExperimentSpec) -> dict:
    limit = spec.inference.configuration.get("max_model_steps")
    if limit is not None and (
        spec.inference.backend is not InferenceBackend.REPLAY
        or type(limit) is not int or limit < 1
    ):
        raise ValueError("inference.configuration.max_model_steps requires replay and a positive integer")
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
                from .openclaw_driver import TOOL_VM_TOOLS, RUNTIME_LOCAL_TOOLS
                unsupported = set(info["response_tool_names"]) - set((*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS))
                if unsupported:
                    raise ValueError(f"{case.case_id}: replay tools unavailable in OpenClaw: {sorted(unsupported)}; adapt the trace tool interface before running")
                if (not info["model_calls"] or not info["model_requests_present"]
                        or not info["model_responses_present"]):
                    raise ValueError(f"{case.case_id}: managed replay requires recorded model requests and responses")
            traces.append({"case_id": case.case_id, **info})
    files = []
    admissions = {policy.admission for policy in spec.policies}
    for policy, source in (
        (AdmissionPolicy.TOOL_ORACLE, spec.resources.oracle_measurements),
    ):
        if policy in admissions:
            path = Path(str(source))
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"{path}: prediction data must be a JSON object")
            provider = CommandPredictionProvider(path)
            if not provider.manifest:
                raise ValueError(f"{path}: managed prediction data contains no command records")
            files.append(str(path))
    if AdmissionPolicy.TOOL_P50 in admissions and spec.resources.prediction_artifact:
        path = Path(spec.resources.prediction_artifact)
        for case in cases:
            provider = P50PredictionProvider(path, repository=case.repository or case.case_id,
                sandbox_identity={k: getattr(spec.sandbox, k) for k in ("image_digest", "vcpu", "memory_mib", "architecture")})
            if not provider.manifest:
                raise ValueError("P50 artifact contains no commands")
            if spec.inference.backend is InferenceBackend.REPLAY:
                from .training import trace_commands
                from .prediction import command_sha256
                missing = [c for c in trace_commands(Path(case.replay_trace_reference or case.source_reference))
                           if command_sha256(c) not in provider.manifest]
                if missing:
                    raise ValueError(f"P50 artifact missing {len(missing)} replay commands")
        files.append(str(path))
    return {"traces": traces, "prediction_files": files}
