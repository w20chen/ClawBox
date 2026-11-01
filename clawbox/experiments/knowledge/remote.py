"""Runtime event adapter: all prediction and learning state belongs to the host."""
import threading

import httpx

from clawbox.experiments.knowledge.protocol import PROTOCOL, command_digest, validate_message
from clawtune_sidecar.contracts.models import ToolPrediction
from clawtune_sidecar.predictors.tool_resource import _command_for_request


_DEFERRED_TIMEOUT_PREFIX = 'CLAWBOX_DEFERRED_TOOL_TIMEOUT_SECONDS='


def _host_profile_command(command):
    """Match the logical command used later by the ClawBox SSH policy shim."""
    if not command.startswith(_DEFERRED_TIMEOUT_PREFIX):
        return command
    marker, separator, remainder = command.partition('\n')
    if not separator:
        return command
    try:
        timeout = float(marker.removeprefix(_DEFERRED_TIMEOUT_PREFIX))
    except ValueError:
        return command
    return remainder if timeout > 0 else command


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
        # The gateway-only timeout marker controls deferred waiting but is not
        # part of the command that the policy shim admits or the Tool executes.
        # Predict from that same logical command so its receipt can be claimed.
        command = _host_profile_command(command)
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
