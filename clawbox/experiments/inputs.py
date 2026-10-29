"""Read-only input checks shared by the public CLI before starting VMs."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .spec import ExperimentSpec, load_workload_cases, resolve_case_sandbox
from .spec_types import AdmissionPolicy, AgentDriver, InferenceBackend
from .prediction import prediction_input_paths, prediction_provider_type
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
        "model_requests_present": all(action.input_blob is not None for action in actions if action.kind == "llm"),
        "model_responses_present": all(action.output is not None for action in actions if action.kind == "llm"),
        "response_tool_names": sorted(tool_names),
        "ends_with_tool_request": last_model_requests_tools,
    }


def _saved_trace_info(case, path: Path) -> dict | None:
    """Reuse an explicit readiness record without rereading an immutable trace."""
    metadata = dict(case.metadata or {})
    saved = metadata.get("input_validation")
    if not isinstance(saved, dict) or saved.get("path") != str(path):
        return None
    digest = saved.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"{case.case_id}: saved replay trace is missing or empty: {path}")
    return {
        "path": str(path),
        "sha256": digest,
        "actions": saved.get("actions"),
        "model_calls": saved.get("model_calls"),
        "tool_calls": saved.get("tool_calls"),
        "model_wait_seconds": saved.get("model_wait_seconds"),
        "model_requests_present": saved.get("model_requests_present"),
        "model_responses_present": saved.get("model_responses_present"),
        "response_tool_names": list(saved.get("response_tool_names") or []),
        "ends_with_tool_request": saved.get("ends_with_tool_request"),
        "identity_source": "workload.metadata.input_validation",
        "content_rehashed": False,
    }


def _validate_openclaw_trace_summary(spec, case, info: dict, limit: int | None) -> None:
    from .openclaw_driver import TOOL_VM_TOOLS, RUNTIME_LOCAL_TOOLS

    unsupported = set(info["response_tool_names"]) - set((*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS))
    if unsupported:
        raise ValueError(
            f"{case.case_id}: replay tools unavailable in OpenClaw: "
            f"{sorted(unsupported)}; adapt the trace tool interface before running"
        )
    if (
        not info["model_calls"]
        or not info["model_requests_present"]
        or not info["model_responses_present"]
    ):
        raise ValueError(
            f"{case.case_id}: managed replay requires recorded model requests and responses"
        )
    if limit is None and info["ends_with_tool_request"]:
        raise ValueError(
            f"{case.case_id}: full replay ends with a tool request and has no final model response; "
            "supply the complete trace, or explicitly configure max_model_steps for a prefix experiment"
        )


def validate_launch_inputs(spec: ExperimentSpec) -> dict:
    """Perform the checks needed to launch, reusing explicit readiness evidence.

    ``validate --inputs`` remains the byte-level audit.  Normal launch avoids
    reparsing every immutable replay and resolving every recorded command before
    the real per-environment qualification executes.
    """
    if spec.schema_version == 4:
        from .spec import effective_spec

        results = [validate_launch_inputs(effective_spec(spec, p)) for p in spec.policies]
        return {
            "traces": results[0]["traces"],
            "prediction_files": sorted({
                path for result in results for path in result["prediction_files"]
            }),
        }
    limit = spec.inference.configuration.get("max_model_steps")
    if limit is not None and (
        spec.inference.backend is not InferenceBackend.REPLAY
        or type(limit) is not int
        or limit < 1
    ):
        raise ValueError(
            "inference.configuration.max_model_steps requires replay and a positive integer"
        )
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
    traces = []
    for case in cases:
        if spec.agent.driver is AgentDriver.OPENCLAW and not case.prompt.strip():
            raise ValueError(f"{case.case_id}: OpenClaw requires a nonempty task prompt")
        if spec.inference.backend is not InferenceBackend.REPLAY:
            continue
        path = Path(case.replay_trace_reference or case.source_reference)
        info = _saved_trace_info(case, path) or inspect_trace(path)
        if spec.agent.driver is AgentDriver.OPENCLAW:
            _validate_openclaw_trace_summary(spec, case, info, limit)
        traces.append({"case_id": case.case_id, **info})

    files = []
    admissions = {policy.admission for policy in spec.policies}
    if spec.resources.prediction_artifact and (
        AdmissionPolicy.PREDICTED in admissions or spec.resources.observe_predictions
    ):
        path = Path(spec.resources.prediction_artifact)
        first = cases[0]
        tool_sandbox = first.sandbox or spec.sandbox
        provider = prediction_provider_type(spec.resources.prediction_mode)(
            path,
            repository=first.repository or first.case_id,
            sandbox_identity={
                key: getattr(tool_sandbox, key)
                for key in (
                    "template_id", "image_digest", "vcpu", "memory_mib", "architecture",
                )
            },
        )
        if spec.resources.prediction_mode == "frozen" and not provider.manifest:
            raise ValueError("P50 artifact contains no commands")
        files.extend(str(value) for value in prediction_input_paths(
            path, spec.resources.prediction_mode,
        ))
    return {"traces": traces, "prediction_files": files}


def validate_inputs(spec: ExperimentSpec) -> dict:
    if spec.schema_version == 4:
        from .spec import effective_spec
        results = [validate_inputs(effective_spec(spec, p)) for p in spec.policies]
        return {'traces': results[0]['traces'],
                'prediction_files': sorted({path for result in results for path in result['prediction_files']})}
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
                _validate_openclaw_trace_summary(spec, case, info, limit)
            traces.append({"case_id": case.case_id, **info})
    files = []
    admissions = {policy.admission for policy in spec.policies}
    if spec.resources.prediction_artifact and (AdmissionPolicy.PREDICTED in admissions or spec.resources.observe_predictions):
        path = Path(spec.resources.prediction_artifact)
        for case in cases:
            tool_sandbox = resolve_case_sandbox(case, spec.sandbox)
            provider = prediction_provider_type(spec.resources.prediction_mode)(path, repository=case.repository or case.case_id,
                sandbox_identity={k: getattr(tool_sandbox, k) for k in (
                    "template_id", "image_digest", "vcpu", "memory_mib", "architecture",
                )})
            if spec.resources.prediction_mode == "frozen" and not provider.manifest:
                raise ValueError("P50 artifact contains no commands")
            if spec.inference.backend is InferenceBackend.REPLAY:
                from .training import trace_commands
                from .prediction import command_sha256
                commands = trace_commands(Path(case.replay_trace_reference or case.source_reference))
                if spec.resources.prediction_mode in {"runtime", "cold-start"}:
                    for command in commands:
                        provider.resolve(command, {"raw_command_sha256": command_sha256(command)})
                else:
                    missing = [c for c in commands if command_sha256(c) not in provider.manifest]
                    if missing:
                        raise ValueError(f"P50 artifact missing {len(missing)} replay commands")
        files.extend(str(value) for value in prediction_input_paths(
            path, spec.resources.prediction_mode,
        ))
    return {"traces": traces, "prediction_files": files}
