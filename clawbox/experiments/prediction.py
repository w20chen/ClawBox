"""Immutable command-specific prediction provenance for managed Tool calls."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from threading import Lock
from typing import Any

from clawbox.tuning.clawtune import shell_command_prefix_tokens


class PredictionUnavailable(RuntimeError):
    """Raised when a managed Tool call has no command-specific prediction."""


def clawtune_extra_peak(prediction: dict[str, Any] | None) -> dict[str, Any]:
    """Convert ClawTune's tool-call memory increment to a reservation."""
    if not isinstance(prediction, dict):
        raise PredictionUnavailable("ClawTune call prediction is missing")
    if (prediction.get("schema_version") != "call_load.v2"
            or prediction.get("scope") != "tool_call"):
        raise PredictionUnavailable("ClawTune call prediction has an incompatible schema")
    measurement = prediction.get("memory_measurement")
    if measurement != "guest_memtotal_minus_memavailable":
        raise PredictionUnavailable(
            "ClawTune memory prediction does not use the Cube guest environment measurement"
        )
    targets = prediction.get("targets")
    target = targets.get("memory_extra_peak_bytes") if isinstance(targets, dict) else None
    if isinstance(target, dict) and target.get("backend") not in {"lattice", "runtime"}:
        raise PredictionUnavailable("ClawBox scheduling requires a LatticeKB or ToolKB memory prediction")
    if (not isinstance(target, dict) or target.get("status") != "available"
            or target.get("unit") != "bytes"
            or target.get("metric_definition") != "environment_memory_peak_minus_baseline"):
        reason = target.get("unavailable_reason") if isinstance(target, dict) else "missing target"
        raise PredictionUnavailable(
            f"ClawTune extra memory peak is unavailable ({reason}); "
            "provide Cube environment-memory observations or select a fixed/capacity baseline"
        )
    value = target.get("p50")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0:
        raise PredictionUnavailable("ClawTune extra memory peak has no valid estimate")
    return {
        "prediction_source": f"clawtune_{target.get('backend')}_p50",
        "prediction_quantile": 0.5,
        "admission_prediction_target": "environment_memory_peak_minus_baseline",
        # Preserve the canonical prediction for the ClawTune trace writer;
        # the derived fields below are the admission-facing projection.
        "call_prediction": prediction,
        "predicted_incremental_memory_mib": value / (1024 * 1024),
        "clawtune_memory_extra_peak_bytes": value,
        "clawtune_memory_backend": target.get("backend"),
        "clawtune_memory_method": target.get("method"),
        "clawtune_memory_sample_count": target.get("sample_count"),
        "clawtune_memory_measurement": measurement,
    }


def command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()


def select_p50(models: dict[str, Any], *, short_call_evidence: dict | None = None) -> dict:
    """ClawBox's admission policy; never change ClawTune's model outputs."""
    missing = {}
    # ``tool_class`` is a separately queried native ToolKB prediction with
    # ``command=None``.  It uses measured calls from the same outer tool
    # (``exec``) when a compound command has no transferable binary identity.
    # Keep it after command-aware models so coarse evidence never masks a
    # clause or single-binary prediction.
    for name in ("lattice", "exact_command", "tool", "tool_class"):
        try:
            result = clawtune_extra_peak(models.get(name))
        except PredictionUnavailable as exc:
            missing[name] = str(exc)
            continue
        expected_backend = "lattice" if name == "lattice" else "runtime"
        if result["clawtune_memory_backend"] != expected_backend:
            raise PredictionUnavailable(f"{name} prediction backend mismatch")
        return {**result, "fallback_level": name, "unavailable_models": missing}
    raise PredictionUnavailable(f"No P50 memory estimate (short duration does not establish zero memory): {missing}")


class FrozenPredictionProvider:
    """Load one immutable prediction artifact once and resolve exact commands."""

    def __init__(self, path: Path, *, repository: str | None = None,
                 prediction_source: str = "runtime_clawtune_immutable_kb") -> None:
        self.path = path
        self.repository = repository
        self.prediction_source = prediction_source
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Prediction artifact must be an object")
        self.payload = payload
        self._by_digest: dict[str, dict[str, Any]] = {}
        self._load_entries(payload)
        self._fallback_counts: dict[str, int] = {}
        self._lock = Lock()

    def _load_entries(self, payload: dict[str, Any]) -> None:
        candidates = payload.get('commands')
        if not isinstance(candidates, list):
            raise ValueError('prediction artifact requires a commands list')
        for item in candidates:
            if not isinstance(item, dict):
                raise ValueError('prediction commands must be objects')
            command = item.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError('prediction commands require nonempty command text')
            digest = str(item.get("command_sha256") or command_sha256(command))
            metadata = self._metadata(item, command, digest)
            previous = self._by_digest.get(digest)
            if previous is not None and previous != metadata:
                raise ValueError(f"Prediction artifact has conflicting entries for command {digest}")
            self._by_digest[digest] = metadata

    def _metadata(self, item: dict[str, Any], command: str,
                  digest: str) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def manifest(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._by_digest.items()}

    @property
    def max_incremental_memory_mib(self) -> float:
        """Largest frozen reservation this artifact can request."""
        values = [
            float(item["predicted_incremental_memory_mib"])
            for item in self._by_digest.values()
        ]
        if not values:
            raise ValueError("Prediction artifact contains no command reservations")
        return max(values)

    def resolve(self, command: str, runtime_metadata: dict[str, Any] | None) -> dict[str, Any]:
        return self.resolve_digest(command_sha256(command), runtime_metadata)

    def resolve_digest(self, digest: str,
                       runtime_metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Resolve metadata without moving raw command text off the SSH path."""
        expected = self._by_digest.get(digest)
        if expected is None:
            with self._lock:
                self._fallback_counts["missing_command"] = self._fallback_counts.get("missing_command", 0) + 1
            raise PredictionUnavailable(
                f"no command-specific Runtime prediction for command sha256 {digest}"
            )
        if not isinstance(runtime_metadata, dict):
            with self._lock:
                self._fallback_counts["runtime_metadata_missing"] = self._fallback_counts.get("runtime_metadata_missing", 0) + 1
            raise PredictionUnavailable("Runtime did not provide command prediction metadata")
        if runtime_metadata.get("raw_command_sha256") != digest:
            raise PredictionUnavailable("Runtime prediction command identity mismatch")
        if runtime_metadata.get("canonical_prediction_key") != expected["canonical_prediction_key"]:
            raise PredictionUnavailable("Runtime prediction canonical key mismatch")
        if runtime_metadata.get("prediction_source") != expected["prediction_source"]:
            raise PredictionUnavailable("Runtime prediction source mismatch")
        return {
            **expected,
            "session_runtime_metadata": dict(runtime_metadata),
        }

    def provenance(self, observed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        observed = observed or []
        levels: dict[str, int] = {}
        for item in observed:
            level = str(item.get("fallback_level") or "unknown")
            levels[level] = levels.get(level, 0) + 1
        with self._lock:
            fallback_counts = dict(self._fallback_counts)
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "entry_count": len(self._by_digest),
            "fallback_counts": fallback_counts,
            "observed_prediction_count": len(observed),
            "observed_fallback_levels": levels,
            "observed_fallback_rate": (
                sum(value for key, value in levels.items() if key != "exact_command")
                / len(observed) if observed else None
            ),
        }


class P50PredictionProvider(FrozenPredictionProvider):
    """Frozen, pre-evaluation native model predictions, keyed by exact command."""

    def __init__(self, path: Path, *, repository: str, sandbox_identity: dict | None = None,
                 global_cold_start: bool = False) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        repository_matches = payload.get("repository") == repository
        if global_cold_start:
            repository_matches = (
                payload.get("repository") == "*"
                and payload.get("prediction_scope") == "clawtune_cold_start"
            )
        if payload.get("schema") != "clawbox_prediction_v2" or not repository_matches:
            raise ValueError("P50 artifact schema or repository mismatch")
        if payload.get("training_validated") is not True:
            raise ValueError("P50 artifact requires a validated Cube training run")
        if sandbox_identity is not None and payload.get("sandbox_identity") != sandbox_identity:
            if not global_cold_start or not _same_cold_start_environment(
                payload.get("sandbox_identity"), sandbox_identity,
            ):
                raise ValueError("P50 training and evaluation Tool environments differ")
        super().__init__(path, repository=repository, prediction_source="frozen_clawbox_p50")
        self.evaluation_sandbox_identity = sandbox_identity
        self.global_cold_start = global_cold_start

    def provenance(self, observed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        selected = [row for row in observed or []
                    if row.get("prediction_source") == self.prediction_source]
        result = super().provenance(selected)
        if self.global_cold_start:
            result.update(
                prediction_scope="clawtune_cold_start",
                calibration_sandbox_identity=self.payload.get("sandbox_identity"),
                evaluation_sandbox_identity=self.evaluation_sandbox_identity,
            )
        result["observed_fallback_rate"] = (
            sum(row.get("fallback_level") != "lattice" for row in selected) / len(selected)
            if selected else None
        )
        return result

    def resolve_digest(self, digest: str,
                       runtime_metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Use Runtime only as the raw-command identity witness.

        Current ClawTune attaches its live ``call_prediction`` plus the raw
        command digest.  The frozen model choice and reservation remain host
        artifact data; requiring Runtime to echo host-only provenance fields
        rejects valid current OpenClaw requests.
        """
        expected = self._by_digest.get(digest)
        if expected is None:
            with self._lock:
                self._fallback_counts["missing_command"] = (
                    self._fallback_counts.get("missing_command", 0) + 1
                )
            raise PredictionUnavailable(
                f"no command-specific P50 prediction for command sha256 {digest}"
            )
        if not isinstance(runtime_metadata, dict):
            with self._lock:
                self._fallback_counts["runtime_metadata_missing"] = (
                    self._fallback_counts.get("runtime_metadata_missing", 0) + 1
                )
            raise PredictionUnavailable("Runtime did not provide command identity metadata")
        if runtime_metadata.get("raw_command_sha256") != digest:
            raise PredictionUnavailable("Runtime prediction command identity mismatch")
        canonical = runtime_metadata.get("canonical_prediction_key")
        if canonical is not None and canonical != expected["canonical_prediction_key"]:
            raise PredictionUnavailable("Runtime prediction canonical key mismatch")
        source = runtime_metadata.get("prediction_source")
        if source is not None and source != expected["prediction_source"]:
            raise PredictionUnavailable("Runtime prediction source mismatch")
        return {**expected, "session_runtime_metadata": dict(runtime_metadata)}

    @property
    def manifest(self) -> dict[str, dict[str, Any]]:
        # The Runtime witnesses command identity; model evidence stays on the
        # host. Avoid sending a large KB/prediction body through guest exec args.
        return {key: {field: value[field] for field in
                      ("raw_command_sha256", "canonical_prediction_key", "prediction_source")}
                for key, value in self._by_digest.items()}

    def _metadata(self, item: dict, command: str, digest: str) -> dict:
        if digest != command_sha256(command):
            raise ValueError("P50 artifact command digest mismatch")
        selected = select_p50(item.get("models") or {},
                              short_call_evidence=item.get("short_call_evidence"))
        calibration = self.payload.get('host_calibration') or {}
        if (calibration.get('target') != 'host_cgroup_memory_current_increment'
                or calibration.get('sample_count', 0) <= 0):
            raise PredictionUnavailable('artifact requires measured host cgroup calibration')
        scale, offset = calibration.get('scale'), calibration.get('offset_bytes')
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in (scale, offset)):
            raise PredictionUnavailable('invalid host calibration')
        guest_mib = selected['predicted_incremental_memory_mib']
        return {**selected, 'predicted_guest_extra_memory_mib': guest_mib,
                'predicted_incremental_memory_mib': guest_mib * scale + offset / 1024**2,
                'admission_prediction_target': 'host_cgroup_memory_current_increment',
                'host_calibration': calibration,
                "model_prediction_source": selected["prediction_source"],
                "prediction_source": self.prediction_source,
                "raw_command_sha256": digest, "canonical_prediction_key": digest,
                "models": item.get("models"), "kb_sha256": self.payload.get("kb_sha256")}


class RuntimePredictionProvider(P50PredictionProvider):
    """Query a validated, immutable training KB for the actual incoming command.

    Model state is frozen for comparable trials, but commands need not occur in
    the training artifact's replay list. Evaluation observations never train it.
    """

    def __init__(self, path: Path, **kwargs):
        super().__init__(path, **kwargs)
        self.prediction_source = "runtime_clawtune_immutable_kb"
        self.kb_path = path.with_suffix(".kb.json")
        try:
            data = self.kb_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"runtime prediction requires {self.kb_path}; copy both experiment train outputs") from exc
        if hashlib.sha256(data).hexdigest() != self.payload.get("kb_sha256"):
            raise ValueError("runtime prediction KB hash differs from training artifact; copy the matching .kb.json")
        from clawbox.clawtune_integration import use_clawtune
        use_clawtune()
        from tool_time.lattice_kb import LatticeTimeKB
        from tool_resource.runtime_kb import RuntimeToolResourceKB
        snapshot = json.loads(data)
        try:
            self.lattice = LatticeTimeKB.from_json_obj(snapshot["lattice"])
            self.runtime = RuntimeToolResourceKB.from_json_obj(snapshot["tool"])
        except (KeyError, ValueError) as exc:
            raise ValueError("training KB cannot be loaded by the selected ClawTune; use the matching ClawTune environment or retrain: " + str(exc)) from exc
        self._query_lock = Lock()

    def _load_entries(self, payload):
        # Do not use or require a precomputed exact-command table in live mode.
        pass

    @property
    def manifest(self):
        return {}

    def resolve(self, command, runtime_metadata):
        if not isinstance(command, str) or not command.strip():
            raise PredictionUnavailable("runtime prediction requires the actual Tool command")
        digest = command_sha256(command)
        if not isinstance(runtime_metadata, dict) or runtime_metadata.get("raw_command_sha256") != digest:
            raise PredictionUnavailable("Runtime prediction command identity mismatch")
        from tool_resource.runtime_kb import ToolCallQuery
        from clawbox.tuning.clawtune import predict_native_call_load_models
        query = ToolCallQuery(repo=self.repository, tool_name="exec", command=command,
                             ts_start=time.time(), memory_measurement="guest_memtotal_minus_memavailable")
        with self._query_lock:
            result = predict_native_call_load_models(self.runtime, query, lattice=self.lattice)
            models = {key: result[key].model_dump(mode="json") for key in ("lattice", "tool")}
            tool_class_query = ToolCallQuery(
                repo=self.repository, tool_name="exec", command=None,
                ts_start=query.ts_start,
                memory_measurement="guest_memtotal_minus_memavailable",
            )
            tool_class = predict_native_call_load_models(
                self.runtime, tool_class_query, lattice=self.lattice,
            )["tool"]
            models["tool_class"] = tool_class.model_dump(mode="json")
            prediction = self._metadata({"models": models}, command, digest)
        return {**prediction, "session_runtime_metadata": dict(runtime_metadata)}

    def resolve_digest(self, digest, runtime_metadata):
        raise PredictionUnavailable("runtime prediction requires command text, not a frozen digest lookup")


def _same_cold_start_environment(calibration: Any, evaluation: Any) -> bool:
    """Permit a resized template only when the executable image is unchanged.

    Host calibration maps incremental guest memory to incremental cgroup memory.
    Template identity and the configured memory ceiling do not change that
    mapping; image, architecture and vCPU shape do.
    """
    if not isinstance(calibration, dict) or not isinstance(evaluation, dict):
        return False
    return all(calibration.get(key) == evaluation.get(key)
               for key in ("image_digest", "architecture", "vcpu"))


class ColdStartPredictionProvider(RuntimePredictionProvider):
    """Query one immutable ClawTune bootstrap KB for any trace repository."""

    def __init__(self, path: Path, **kwargs):
        super().__init__(path, global_cold_start=True, **kwargs)
        self.prediction_source = "runtime_clawtune_cold_start_kb"


def prediction_provider_type(mode: str):
    if mode == "runtime":
        return RuntimePredictionProvider
    if mode == "cold-start":
        return ColdStartPredictionProvider
    return P50PredictionProvider
