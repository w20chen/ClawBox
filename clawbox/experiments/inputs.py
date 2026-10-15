"""Read-only input checks shared by the public CLI before starting VMs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .spec import ExperimentSpec, load_workload_cases
from .spec_types import AdmissionPolicy, AgentDriver, InferenceBackend
from .prediction import prediction_provider_type
from clawbox.replay.trace import load_trace


def inspect_trace(path: Path) -> dict:
    actions = load_trace(path)
    ids = [action.action_id for action in actions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: action IDs must be unique")
    tool_names = set()
    last_model_requests_tools = False
    for action in actions:
        if action.kind != "llm" or not isinstance(action.output, dict):
            continue
        message = action.output
        if isinstance(message.get("content"), dict):
            message = message["content"]
        last_model_requests_tools = bool(message.get("tool_calls"))
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
        "ends_with_tool_request": last_model_requests_tools,
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
    if spec.agent.driver is AgentDriver.OPENCLAW:
        from .llm_config import resolve_llm_configuration

        configuration, _ = resolve_llm_configuration(
            spec.inference.configuration, live=False,
        )
        model = str(
            configuration.get("model") or os.getenv("OPENCLAW_MODEL_REF", "")
        ).strip()
        if not model:
            raise ValueError(
                "OpenClaw requires inference.configuration.model (or "
                "OPENCLAW_MODEL_REF); configure it before any VMs are started"
            )
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
                if limit is None and info["ends_with_tool_request"]:
                    raise ValueError(
                        f"{case.case_id}: full replay ends with a tool request and has no final model response; "
                        "supply the complete trace, or explicitly configure max_model_steps for a prefix experiment"
                    )
            traces.append({"case_id": case.case_id, **info})
    files = []
    admissions = {policy.admission for policy in spec.policies}
    if any(policy.runtime_control.value != 'off' for policy in spec.policies):
        from .congestion import UPSTREAM_SHA256, UPSTREAM_SOURCE
        source = Path(spec.resources.agentcgroup_source) if spec.resources.agentcgroup_source else UPSTREAM_SOURCE
        if hashlib.sha256(source.read_bytes()).hexdigest() != UPSTREAM_SHA256:
            raise ValueError("AgentCgroup source does not match the pinned backend")
    if spec.resources.prediction_artifact and (AdmissionPolicy.PREDICTED in admissions or spec.resources.observe_predictions):
        path = Path(spec.resources.prediction_artifact)
        for case in cases:
            tool_sandbox = case.sandbox or spec.sandbox
            provider = prediction_provider_type(spec.resources.prediction_mode)(path, repository=case.repository or case.case_id,
                sandbox_identity={k: getattr(tool_sandbox, k) for k in ("image_digest", "vcpu", "memory_mib", "architecture")})
            if spec.resources.prediction_mode == "frozen" and not provider.manifest:
                raise ValueError("P50 artifact contains no commands")
            if spec.inference.backend is InferenceBackend.REPLAY:
                from .training import trace_commands
                from .prediction import command_sha256
                commands = trace_commands(Path(case.replay_trace_reference or case.source_reference))
                if spec.resources.prediction_mode == "runtime":
                    for command in commands:
                        provider.resolve(command, {"raw_command_sha256": command_sha256(command)})
                else:
                    missing = [c for c in commands if command_sha256(c) not in provider.manifest]
                    if missing:
                        raise ValueError(f"P50 artifact missing {len(missing)} replay commands")
        files.append(str(path))
        if spec.resources.prediction_mode == "runtime":
            files.append(str(path.with_suffix(".kb.json")))
    return {"traces": traces, "prediction_files": files}
