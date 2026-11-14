"""Offline replay preparation and trace-free CPU input identities."""
from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from clawbox.replay.trace import _read_jsonl, load_trace


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def catalog_digest(identities: list[dict]) -> str:
    """Same path-independent identity as the ClawBox GPU adapter."""
    return hashlib.sha256(_json(sorted(identities, key=lambda row: row["trace_id"])).encode()).hexdigest()


def _task_digest(case) -> str:
    fields = ('case_id', 'prompt', 'repository', 'base_commit', 'sandbox', 'validation')
    task = case.model_dump(mode='json', include=set(fields))
    return hashlib.sha256(_json(task).encode()).hexdigest()


def _prediction_identity(spec, case) -> dict | None:
    from .spec import resolve_case_sandbox
    from .spec_types import AdmissionPolicy
    from .prediction import prediction_input_paths
    resources = spec.resources
    required = (any(p.admission is AdmissionPolicy.PREDICTED for p in spec.policies)
                or resources.observe_predictions or resources.prediction_mode == 'host-live')
    if not required:
        return None
    if not resources.prediction_artifact:
        raise ValueError('prepare-serving-sim requires the configured prediction artifact for predicted admission')
    sandbox = resolve_case_sandbox(case, spec.sandbox)
    paths = prediction_input_paths(Path(resources.prediction_artifact), resources.prediction_mode)
    return dict(mode=resources.prediction_mode, repository=case.repository or case.case_id,
        sandbox={k: getattr(sandbox, k) for k in ('template_id', 'image_digest', 'vcpu', 'memory_mib', 'architecture')},
        files={str(p.resolve()): _file_digest(p) for p in paths})


def _file_digest(path: Path) -> str:
    path = path.resolve()
    stat = path.stat()
    return _cached_file_digest(str(path), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


@lru_cache(maxsize=128)
def _cached_file_digest(path: str, identity: tuple) -> str:
    return _sha(Path(path))


def _prepare_prediction(spec, case, replay: Path) -> dict | None:
    identity = _prediction_identity(spec, case)
    if identity is None:
        return None
    from .prediction import prediction_provider_type, command_sha256
    from .training import trace_commands
    provider = prediction_provider_type(identity['mode'])(Path(spec.resources.prediction_artifact),
        repository=identity['repository'], sandbox_identity=identity['sandbox'])
    commands = trace_commands(replay)
    if identity['mode'] in {'runtime', 'cold-start'}:
        for command in commands:
            provider.resolve(command, {'raw_command_sha256': command_sha256(command)})
    elif identity['mode'] == 'frozen':
        missing = [c for c in commands if command_sha256(c) not in provider.manifest]
        if missing:
            raise ValueError(f'{case.case_id}: P50 artifact missing {len(missing)} replay commands')
    return dict(identity=identity, commands_checked=len(commands))


def prepare_serving_sim(spec, output: Path, *, source_map: dict | None = None) -> dict:
    from .spec import load_workload_cases
    from .inputs import inspect_trace
    from .openclaw_driver import replay_uses_background_execution
    from types import SimpleNamespace

    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError(f"{output}: output already exists; choose a new bundle directory")
    source_map = source_map or {}
    files, cases, catalog, identities, provenance, cpu_cases = {}, [], [], [], [], []
    for case in load_workload_cases(spec.workload):
        replay = Path(case.replay_trace_reference or case.source_reference).resolve()
        actions = load_trace(replay)
        metadata = next((r for r in _read_jsonl(replay) if r.get("record_type") == "trace_metadata"), {})
        source_value = source_map.get(case.case_id) or case.metadata.get("source_trace") or metadata.get("source_trace")
        relative_base = replay.parent
        if not source_value and case.source_reference != str(case.replay_trace_reference):
            source_value = case.source_reference
            relative_base = Path.cwd()
        if not isinstance(source_value, str) or not source_value:
            raise ValueError(f"{case.case_id}: original format-5 source is required; supply --source-map with this case ID")
        source = Path(source_value).expanduser()
        if not source.is_absolute():
            source = relative_base / source
        source = source.resolve()
        if source == replay or not source.is_file():
            raise ValueError(f"{case.case_id}: original format-5 file unavailable at {source}; supply --source-map")
        rows = _read_jsonl(source)
        meta = [r for r in rows if r.get("type") == "trace_metadata"]
        if len(meta) != 1 or meta[0].get("trace_format_version") != 5:
            raise ValueError(f"{source}: expected original canonical format-5 trace")
        source_hash = _sha(source)
        expected = metadata.get("source_sha256") or case.metadata.get("source_sha256")
        if expected and expected != source_hash:
            raise ValueError(f"{case.case_id}: source hash differs from the CPU replay provenance")
        source_actions = [r for r in rows if r.get("type") == "action" and r.get("action_type") == "llm_call"]
        by_id = {r["action_id"]: r for r in source_actions}
        if len(by_id) != len(source_actions):
            raise ValueError(f"{source}: duplicate LLM action IDs; select a single trajectory")
        if any(a.action_id not in by_id for a in actions):
            raise ValueError(f"{case.case_id}: CPU replay actions are absent from the original source")
        agents = {by_id[a.action_id].get("agent_id") for a in actions}
        if len(agents) != 1 or not next(iter(agents)):
            raise ValueError(f"{case.case_id}: selected actions must belong to one explicit source agent")
        agent_id = next(iter(agents))
        replay_hash = _sha(replay)
        trace_id = hashlib.sha256(f"{case.case_id}\0{source_hash}\0{replay_hash}".encode()).hexdigest()
        adapted_meta = copy.deepcopy(meta[0])
        adapted_meta["clawbox_adaptation"] = {
            "source_sha256": source_hash, "replay_sha256": replay_hash,
            "response_source": "CPU replay assistant messages", "token_length_policy": "recorded",
        }
        adapted = [adapted_meta]
        missing_reasoning = 0
        source_models = {meta[0]["model"]} if isinstance(meta[0].get("model"), str) and meta[0]["model"] else set()
        for index, action in enumerate(actions):
            row = copy.deepcopy(by_id[action.action_id])
            data = row["data"]
            raw = data["raw_response"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            source_models.update(value for value in (data.get("model"), raw.get("model")) if isinstance(value, str) and value)
            if len(raw.get("choices", [])) != 1:
                raise ValueError(f"{source}:{action.action_id}: exactly one model choice is required")
            count = data.get("completion_tokens")
            if type(count) is not int or count <= 0:
                raise ValueError(f"{source}:{action.action_id}: positive recorded completion_tokens required")
            reasoning = data.get("reasoning_output_tokens", (raw.get("usage", {}).get("completion_tokens_details") or {}).get("reasoning_tokens"))
            if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= count):
                raise ValueError(f"{source}:{action.action_id}: invalid recorded reasoning count")
            missing_reasoning += reasoning is None
            if not raw["choices"][0].get("finish_reason"):
                raise ValueError(f"{source}:{action.action_id}: recorded finish_reason required")
            if not isinstance(action.output, dict):
                raise ValueError(f"{replay}:{action.action_id}: structured assistant message required")
            # CPU replay is the executable contract: reuse its already adapted
            # tools/paths exactly, while preserving source token counts and timing.
            raw["choices"][0]["message"] = action.output
            data["raw_response"] = raw
            data["messages_in"] = action.input
            row["clawbox_source_iteration"] = row.get("iteration")
            row["iteration"] = index
            adapted.append(row)
        relative = f"traces/{trace_id}.jsonl"
        content = "".join(_json(row) + "\n" for row in adapted).encode("utf-8")
        files[relative] = content
        digest = hashlib.sha256(content).hexdigest()
        catalog.append(dict(trace_id=trace_id, path=relative, agent_id=agent_id))
        identities.append(dict(trace_id=trace_id, agent_id=agent_id, sha256=digest))
        provenance.append(dict(case_id=case.case_id, trace_id=trace_id, agent_id=agent_id,
                          source_path=str(source), source_sha256=source_hash,
                          replay_path=str(replay), replay_sha256=replay_hash,
                          gpu_trace_path=relative, gpu_trace_sha256=digest,
                          source_model=next(iter(source_models)) if len(source_models) == 1 else None,
                          source_models=sorted(source_models), model_calls=len(actions),
                          action_ids=[a.action_id for a in actions], missing_reasoning_calls=missing_reasoning))
        summary = inspect_trace(replay)
        for key in ('path', 'sha256', 'model_wait_seconds'):
            summary.pop(key)
        recorded_duration = case.metadata.get('recorded_duration_seconds')
        if recorded_duration is None:
            recorded_duration = max(a.start_s + a.duration_s for a in actions) - min(a.start_s for a in actions)
        cases.append(dict(case_id=case.case_id, trace_id=trace_id, agent_id=agent_id,
            gpu_trace_sha256=digest, task_digest=_task_digest(case), model_calls=len(actions),
            source_models=sorted(source_models), missing_reasoning_calls=missing_reasoning,
            allow_background_execution=replay_uses_background_execution(SimpleNamespace(
                gateway=SimpleNamespace(mode='replay', actions=actions))),
            recorded_duration_seconds=recorded_duration, input_validation=summary,
            prediction_validation=_prepare_prediction(spec, case, replay)))
        cpu_case = case.model_dump(mode='json')
        cpu_case.pop('replay_trace_reference', None)
        cpu_case['source_reference'] = str(output / 'workload.json')
        cpu_case['metadata'] = {k: v for k, v in case.metadata.items()
                                if k not in {'input_validation', 'source_trace', 'source_sha256'}}
        cpu_case['metadata']['recorded_duration_seconds'] = recorded_duration
        cpu_cases.append(cpu_case)
    if len({row["case_id"] for row in cases}) != len(cases):
        raise ValueError("serving simulator bundle requires unique case IDs")
    files["catalog.json"] = (_json(dict(traces=catalog)) + "\n").encode()
    manifest = dict(schema_version=2, kind="clawbox_serving_sim_cpu_manifest", token_length_policy="recorded",
                    catalog=dict(catalog_digest=catalog_digest(identities)), cases=cases)
    files['gpu-provenance.json'] = (_json(dict(cases=provenance)) + '\n').encode()
    files['workload.json'] = (json.dumps(cpu_cases, indent=2) + '\n').encode()
    output.mkdir(parents=True)
    for relative, content in files.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dict(manifest=str(output / "manifest.json"), catalog=str(output / "catalog.json"),
                workload=str(output / 'workload.json'),
                cases=len(cases), catalog_digest=manifest["catalog"]["catalog_digest"])


@lru_cache(maxsize=16)
def _read_manifest(path: str, identity: tuple) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 2 or document.get("kind") != "clawbox_serving_sim_cpu_manifest":
        raise ValueError(f"{path}: expected CPU manifest version 2; run prepare-serving-sim into a new directory")
    rows = document['cases']
    if len({row['case_id'] for row in rows}) != len(rows):
        raise ValueError(f'{path}: duplicate case IDs')
    for row in rows:
        if type(row['model_calls']) is not int or row['model_calls'] < 1:
            raise ValueError(f"{path}: model_calls must be positive")
        if type(row['allow_background_execution']) is not bool:
            raise ValueError(f"{path}: allow_background_execution must be boolean")
    identities = [dict(trace_id=row['trace_id'], agent_id=row['agent_id'], sha256=row['gpu_trace_sha256']) for row in rows]
    if catalog_digest(identities) != document['catalog']['catalog_digest']:
        raise ValueError('serving simulator catalog identity is inconsistent')
    return document


def load_serving_sim_manifest(path, *, verify: bool = False) -> dict:
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    document = _read_manifest(str(path), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return document


@lru_cache(maxsize=16)
def _case_index(path: str, identity: tuple) -> dict:
    return {row['case_id']: row for row in _read_manifest(path, identity)['cases']}


def serving_sim_case_binding(configuration: dict, case_id: str) -> dict:
    path = configuration.get("serving_sim_catalog_manifest")
    if not path:
        raise ValueError("serving_sim requires inference.configuration.serving_sim_catalog_manifest; run experiment prepare-serving-sim")
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    row = _case_index(str(path), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)).get(case_id)
    if row is not None:
        return row
    raise ValueError(f"{case_id}: absent from serving simulator manifest; prepare a bundle for this workload")


def serving_sim_input_identity(spec) -> dict:
    from .spec import load_workload_cases
    from .spec_types import InferenceBackend

    if spec.inference.backend is not InferenceBackend.SERVING_SIM:
        return {}
    configuration = spec.inference.configuration
    path = configuration.get("serving_sim_catalog_manifest")
    if not path:
        raise ValueError("serving_sim requires inference.configuration.serving_sim_catalog_manifest")
    if not configuration.get("base_url"):
        raise ValueError("serving_sim requires inference.configuration.base_url")
    if not isinstance(configuration.get("model"), str) or not configuration["model"].strip():
        raise ValueError("serving_sim requires a nonempty inference.configuration.model")
    if configuration.get("time_scale", 1.0) != 1.0:
        raise ValueError("serving_sim cannot scale recorded delays; remove time_scale or set it to 1")
    manifest = load_serving_sim_manifest(path, verify=True)
    for case in load_workload_cases(spec.workload):
        binding = serving_sim_case_binding(configuration, case.case_id)
        if _task_digest(case) != binding['task_digest']:
            raise ValueError(f"{case.case_id}: CPU task differs from the prepared bundle; prepare a new bundle")
        prediction = _prediction_identity(spec, case)
        receipt = binding.get('prediction_validation')
        if prediction is not None and (receipt is None or receipt.get('identity') != prediction):
            raise ValueError(f"{case.case_id}: prediction inputs changed or were not qualified during preparation; prepare a new bundle")
    return {"serving_sim": dict(manifest_path=str(Path(path).resolve()), manifest_sha256=_sha(Path(path)),
                                catalog_digest=manifest["catalog"]["catalog_digest"])}
