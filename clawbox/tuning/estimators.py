"""Resource/latency estimators + prediction-vs-actual evaluation.

These are pure-Python estimators over the normalized ``ToolObservation`` set,
the same logic ClawTune exposes as shadow predictions.  Making them standalone
and offline-evaluable is what lets the paper report MAE / bucket accuracy /
calibration on real tool-execution data.

Estimators
----------
* ``LatencyEstimator``: per-command p50/p90 latency (seconds) with a fallback
  to the global population when a command has no history.
* ``LatencyBucketClassifier``: short/medium/long bucket classifier trained on
  observed commands (per-command majority bucket with global prior).
* ``MemoryEstimator``: per-command p90 peak RSS (bytes) + a residual quantile
  factor so predictions carry headroom against over-subscription.
* ``CpuEstimator``: per-command p90 average CPU cores.

Evaluation
----------
``evaluate_predictions`` reports MAE, bucket accuracy, calibration (predicted
p90 vs actual coverage), and mean over-allocation headroom.  ``cross_validate``
runs leave-one-command-out evaluation so predictions for commands never seen in
training are measured honestly.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from .schema import ToolObservation, duration_bucket


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * q
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return sorted_values[lower]
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


@dataclass(frozen=True)
class LatencyEstimate:
    p50_sec: float
    p90_sec: float
    bucket: str | None
    sample_count: int
    source: str  # "per-command" | "global-default"


def _sorted_durations(observations: list[ToolObservation]) -> list[float]:
    values = [obs.duration_sec for obs in observations if obs.duration_sec is not None]
    return sorted(values)


class LatencyEstimator:
    """Per-command p50/p90 latency with global fallback."""

    def __init__(self, observations: list[ToolObservation]) -> None:
        self._global = _sorted_durations(observations)
        self._by_command: dict[str, list[float]] = defaultdict(list)
        for obs in observations:
            key = obs.command_digest or obs.command or obs.execution_id
            if obs.duration_sec is not None:
                self._by_command[key].append(obs.duration_sec)
        for key in self._by_command:
            self._by_command[key].sort()

    def predict(self, observation: ToolObservation) -> LatencyEstimate:
        key = observation.command_digest or observation.command
        values = self._by_command.get(key, [])
        if values:
            p50 = _quantile(values, 0.5)
            p90 = _quantile(values, 0.9)
            source = "per-command"
        else:
            p50 = _quantile(self._global, 0.5)
            p90 = _quantile(self._global, 0.9)
            source = "global-default"
        return LatencyEstimate(
            p50_sec=round(p50, 6),
            p90_sec=round(p90, 6),
            bucket=duration_bucket(p90),
            sample_count=len(values),
            source=source,
        )


class LatencyBucketClassifier:
    """Per-command majority latency bucket with a global prior fallback."""

    def __init__(self, observations: list[ToolObservation]) -> None:
        self._global_counts: Counter[str] = Counter()
        self._by_command: dict[str, Counter[str]] = defaultdict(Counter)
        for obs in observations:
            bucket = obs.latency_bucket
            if bucket is None:
                continue
            self._global_counts[bucket] += 1
            key = obs.command_digest or obs.command or obs.execution_id
            self._by_command[key][bucket] += 1

    def predict(self, observation: ToolObservation) -> str | None:
        key = observation.command_digest or observation.command
        command_counts = self._by_command.get(key)
        if command_counts:
            return command_counts.most_common(1)[0][0]
        if self._global_counts:
            return self._global_counts.most_common(1)[0][0]
        return None


class MemoryEstimator:
    """Per-command p90 peak RSS (bytes) with residual-quantile headroom."""

    def __init__(self, observations: list[ToolObservation], residual_quantile: float = 0.5) -> None:
        self._residual_quantile = residual_quantile
        self._global: list[float] = []
        self._by_command: dict[str, list[float]] = defaultdict(list)
        for obs in observations:
            rss = obs.rss_peak_bytes if obs.rss_peak_bytes is not None else obs.memory_rss_bytes_after
            if rss is None:
                continue
            self._global.append(float(rss))
            key = obs.command_digest or obs.command or obs.execution_id
            self._by_command[key].append(float(rss))
        self._global.sort()
        for key in self._by_command:
            self._by_command[key].sort()

    def predict(self, observation: ToolObservation) -> tuple[float, int, str]:
        key = observation.command_digest or observation.command
        values = self._by_command.get(key, [])
        if values:
            p90 = _quantile(values, 0.9)
            source = "per-command"
            sample_count = len(values)
        else:
            p90 = _quantile(self._global, 0.9)
            source = "global-default"
            sample_count = 0
        residual = _quantile(self._global, self._residual_quantile) if self._global else 0.0
        return round(p90 + residual, 2), sample_count, source


class CpuEstimator:
    """Per-command p90 average CPU cores."""

    def __init__(self, observations: list[ToolObservation]) -> None:
        self._global: list[float] = []
        self._by_command: dict[str, list[float]] = defaultdict(list)
        for obs in observations:
            cores = obs.cpu_utilization_avg_cores
            if cores is None:
                continue
            self._global.append(float(cores))
            key = obs.command_digest or obs.command or obs.execution_id
            self._by_command[key].append(float(cores))
        self._global.sort()
        for key in self._by_command:
            self._by_command[key].sort()

    def predict(self, observation: ToolObservation) -> tuple[float, int, str]:
        key = observation.command_digest or observation.command
        values = self._by_command.get(key, [])
        if values:
            return round(_quantile(values, 0.9), 4), len(values), "per-command"
        return round(_quantile(self._global, 0.9), 4), 0, "global-default"


# ── Evaluation ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvaluationMetrics:
    mae_sec: float
    median_abs_error_sec: float
    bucket_accuracy: float
    calibration_p90: float  # fraction of actuals <= predicted p90
    mean_over_allocation_pct: float | None
    n: int

    def summary(self) -> dict[str, float]:
        return {
            "mae_sec": round(self.mae_sec, 6),
            "median_abs_error_sec": round(self.median_abs_error_sec, 6),
            "bucket_accuracy": round(self.bucket_accuracy, 4),
            "calibration_p90": round(self.calibration_p90, 4),
            "mean_over_allocation_pct": round(self.mean_over_allocation_pct, 4)
            if self.mean_over_allocation_pct is not None
            else -1.0,
            "n": float(self.n),
        }


def evaluate_predictions(
    observations: list[ToolObservation],
    estimator: LatencyEstimator,
    bucket_classifier: LatencyBucketClassifier | None = None,
) -> EvaluationMetrics:
    """Compare latency predictions against actuals on a held-out set.

    * MAE / median absolute error over p50 predictions.
    * Bucket accuracy (when a classifier is supplied).
    * Calibration: fraction of actual durations that landed at or below the
      predicted p90.
    * Over-allocation: (predicted_p90 - actual) / actual, averaged over actuals
      with duration > 0 (headroom the scheduler pays for).
    """
    if not observations:
        return EvaluationMetrics(0.0, 0.0, 0.0, 0.0, None, 0)
    errors: list[float] = []
    within_p90: list[bool] = []
    over_allocation: list[float] = []
    bucket_hits: list[bool] = []
    bucket_total = 0
    for observation in observations:
        if observation.duration_sec is None:
            continue
        estimate = estimator.predict(observation)
        errors.append(abs(estimate.p50_sec - observation.duration_sec))
        within_p90.append(observation.duration_sec <= estimate.p90_sec)
        if observation.duration_sec > 0:
            over_allocation.append((estimate.p90_sec - observation.duration_sec) / observation.duration_sec)
        if bucket_classifier is not None:
            predicted = bucket_classifier.predict(observation)
            actual = observation.latency_bucket
            if predicted is not None and actual is not None:
                bucket_hits.append(predicted == actual)
                bucket_total += 1
    errors.sort()
    bucket_accuracy = (sum(bucket_hits) / bucket_total) if bucket_total else 0.0
    return EvaluationMetrics(
        mae_sec=sum(errors) / len(errors) if errors else 0.0,
        median_abs_error_sec=_quantile(errors, 0.5) if errors else 0.0,
        bucket_accuracy=bucket_accuracy,
        calibration_p90=sum(within_p90) / len(within_p90) if within_p90 else 0.0,
        mean_over_allocation_pct=sum(over_allocation) / len(over_allocation) * 100.0
        if over_allocation
        else None,
        n=len(observations),
    )


def cross_validate(
    observations: list[ToolObservation],
    train_frac: float = 0.8,
    seed: int = 42,
) -> dict[str, float]:
    """Leave-some-commands-out evaluation (cold-start generalization).

    Trains on a command-disjoint random sample and evaluates on the held-out
    commands — which the estimators have NEVER seen — so the reported numbers
    reflect true cold-start generalization, not memorization of per-command
    histories.
    """
    from .dataset import command_disjoint_split

    train, eval_ = command_disjoint_split(observations, train_frac=train_frac, seed=seed)
    estimator = LatencyEstimator(train)
    classifier = LatencyBucketClassifier(train)
    metrics = evaluate_predictions(eval_, estimator, classifier)
    return {
        "train_n": float(len(train)),
        "eval_n": float(len(eval_)),
        "cold_start": True,
        **metrics.summary(),
    }
