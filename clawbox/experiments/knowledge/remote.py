"""Runtime event adapter: all prediction and learning state belongs to the host."""
import threading

import httpx

from clawbox.experiments.knowledge.protocol import PROTOCOL, command_digest, validate_message
from clawtune_sidecar.contracts.models import ToolPrediction
from clawtune_sidecar.predictors.tool_resource import _command_for_request


class RemoteKnowledgeClient:
    artifact_dir = None
    _state_store = None
    frozen = True

    def __init__(self, config):
        self.config = config
        self.client = httpx.Client(base_url=config.host_kb_endpoint.rstrip('/'),
            headers={'Authorization': 'Bearer ' + config.host_kb_token},
            timeout=config.host_kb_timeout_seconds, trust_env=False)
        self._execution_start_lock = threading.RLock()
        self.provenance = None

    def predict(self, request):
        command = _command_for_request(request)
        if request.tool_name not in {'exec', 'terminal_exec'} or not command:
            return ToolPrediction()
        payload = dict(schema_version=PROTOCOL, session_id=self.config.host_kb_session_id,
            event_id=request.event_id, tool_call_id=request.tool_call_id,
            tool_name=request.tool_name, command=command, command_sha256=command_digest(command),
            repository=self.config.tool_resource_repo, command_context=request.command_context or {},
            raw_params=request.raw_params or {}, memory_measurement='cgroup_v2_environment_union_v1')
        validate_message(payload, 'predict')
        response = self.client.post('/v1/kb/predict', json=payload)
        response.raise_for_status()
        value = response.json()
        validate_message(value, 'prediction')
        self.provenance = {key: value[key] for key in ('seed_sha256', 'mapping_policy_sha256', 'scope_generation', 'view_revision')}
        return ToolPrediction.model_validate({**value['prediction'], 'tool_resource': {'host_kb': {
            'prediction_receipt': value['prediction_receipt'], 'view_revision': value['view_revision'],
            'tool_name': request.tool_name}}})

    def observe_completion(self, event, sample):
        # The Tool bridge has already committed (or reported degradation).
        return 0

    def record_tool_started(self, request):
        pass

    def finalize_call(self, request):
        pass

    def finalize_runtime(self, runtime_id, gateway_id):
        pass

    def awaiting_tool_completion(self, request):
        return False

    def execution_telemetry(self, execution_id):
        return None

    def execution_active(self, execution_id):
        return False

    def active_execution_ids(self, *args, **kwargs):
        return ()

    def has_active_executions(self):
        return False

    def flush_kb_updates(self, timeout_seconds=None):
        pass

    def kb_updates_pending(self):
        return False

    def close(self, timeout_seconds=None):
        self.client.close()

    def begin_execution(self, **kwargs):
        raise RuntimeError('host-live Runtime cannot collect Tool execution measurements')

    def finish_execution(self, **kwargs):
        raise RuntimeError('host-live Runtime cannot finalize Tool measurements')
