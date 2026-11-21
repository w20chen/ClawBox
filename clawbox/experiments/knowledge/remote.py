"""Runtime event adapter: all prediction and learning state belongs to the host."""
import threading
from collections import OrderedDict

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
        self.receipts = {}
        self.forecast_receipts = OrderedDict()

    def _payload(self, request):
        command = _command_for_request(request)
        if request.tool_name not in {'exec', 'terminal_exec'} or not command:
            return None
        # The gateway-only timeout marker controls deferred waiting but is not
        # part of the command that the policy shim admits or the Tool executes.
        # Predict from that same logical command so its receipt can be claimed.
        command = _host_profile_command(command)
        payload = dict(schema_version=PROTOCOL, session_id=self.config.host_kb_session_id,
            logical_owner={name: getattr(request, name) for name in
                           ('gateway_id', 'runtime_id', 'session_id', 'run_id')},
            event_id=request.event_id, tool_call_id=request.tool_call_id,
            tool_name=request.tool_name, command=command, command_sha256=command_digest(command),
            repository=self.config.tool_resource_repo, command_context=request.command_context or {},
            raw_params=request.raw_params or {}, memory_measurement='cgroup_v2_environment_union_v1')
        validate_message(payload, 'predict')
        return payload

    @staticmethod
    def _unavailable():
        from clawtune_sidecar.predictors.call_load import compose
        from clawtune_sidecar.prediction_config import load_bucket_edges
        from clawtune_sidecar.contracts.load_prediction import summarize_pmu_evidence
        edges = load_bucket_edges((100., 500., 2000., 10000.))
        loads = {name: {'load': compose(backend, (), edges, reason='host_kb_shell_only')}
                 for name, backend in (('tool', 'runtime'), ('trie', 'trie'), ('lattice', 'lattice'))}
        loads['tool']['pmu'] = summarize_pmu_evidence({})
        return ToolPrediction.model_validate(loads)

    @staticmethod
    def _key(request):
        return (request.gateway_id, request.runtime_id, request.event_id)

    def predict(self, request):
        payload = self._payload(request)
        if payload is None:
            return self._unavailable()
        response = self.client.post('/v1/kb/predict', json=payload)
        response.raise_for_status()
        value = response.json()
        validate_message(value, 'prediction')
        self.provenance = {key: value[key] for key in ('seed_sha256', 'mapping_policy_sha256', 'scope_generation', 'view_revision')}
        prediction = ToolPrediction.model_validate(value['prediction'])
        self.receipts[self._key(request)] = {
            'prediction_receipt': value['prediction_receipt'], 'view_revision': value['view_revision'],
            'tool_name': request.tool_name}
        from .session_forecast import call_key
        key = call_key(payload['logical_owner'], request.tool_call_id)
        self.forecast_receipts[key] = value['prediction_receipt']
        self.forecast_receipts.move_to_end(key)
        while len(self.forecast_receipts) > 4096:
            self.forecast_receipts.popitem(last=False)
        return prediction

    def predict_session_load(self, request):
        payload = self._payload(request)
        if payload is None:
            return self._unavailable().lattice.load
        from clawtune_sidecar.contracts.load_prediction import CallLoadPrediction
        response = self.client.post('/v1/kb/preview', json=payload)
        response.raise_for_status()
        return CallLoadPrediction.model_validate(response.json())

    def observe_completion(self, event, sample):
        # The Tool bridge has already committed (or reported degradation).
        return 0

    def record_tool_started(self, request):
        pass

    def finalize_call(self, request):
        self.receipts.pop(self._key(request), None)

    def finalize_runtime(self, runtime_id, gateway_id):
        for key in list(self.forecast_receipts):
            if key[:2] == (gateway_id, runtime_id):
                self.forecast_receipts.pop(key, None)
        for key in list(self.receipts):
            if key[:2] == (gateway_id, runtime_id):
                self.receipts.pop(key, None)

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


class HostReceiptPolicy:
    """Carry execution credentials in the control response, never public predictions."""

    def __init__(self, policy, predictor):
        self.policy, self.predictor = policy, predictor
        self.name, self.version = policy.name, policy.version

    async def decide(self, request, context):
        decision = await self.policy.decide(request, context)
        receipt = self.predictor.receipts.pop(self.predictor._key(request), None)
        if receipt is not None:
            decision.placement = {**(decision.placement or {}), 'host_kb': receipt}
        return decision
