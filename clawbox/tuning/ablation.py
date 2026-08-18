"""Ablation experiment: fixed baseline vs global-only vs KB (ADR-008 §6).

The paper's central claim is that real tool-execution observations improve
resource prediction.  This module measures that improvement honestly:

* ``FixedProfilePredictor`` — the status quo: a constant conservative profile
  (what FixedProfileSizer uses today).  No learning at all.
* ``GlobalOnlyPredictor`` — learns from observations but only the global
  population (no per-command history): isolates the value of the KB layer.
* ``KnowledgeBase`` — full KB: per-command history with global fallback.

All three are evaluated on the SAME held-out, command-disjoint eval set, so the
reported MAE / bucket accuracy / calibration delta isolates the contribution of
each layer.  Shadow predictions never change real resource sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .estimators import LatencyBucketClassifier, LatencyEstimator, evaluate_predictions
from .kb import KnowledgeBase, Prediction
from .schema import ToolObservation, duration_bucket


class Predictor(Protocol):
    def predict(self, observation: ToolObservation) -> Prediction: ...


@dataclass(frozen=True)
class FixedProfilePredictor:
    """Constant conservative profile (the no-learning control)."""

    latency_p90_sec: float = 60.0
    cpu_p90_cores: float = 1.0
    memory_p90_bytes: int = 2 * 1024**3  # 2 GiB, the small tool profile

    def predict(self, observation: ToolObservation) -> Prediction:
        return Prediction(
            latency_p50_sec=self.latency_p90_sec * 0.7,
            latency_p90_sec=self.latency_p90_sec,
            time_bucket=duration_bucket(self.latency_p90_sec),
            cpu_p90_cores=self.cpu_p90_cores,
            memory_p90_bytes=float(self.memory_p90_bytes),
            sample_count=0,
            match_level="fixed-profile",
            confidence=0.0,
        )


@dataclass(frozen=True)
class GlobalOnlyPredictor:
    """Learns the global population but NOT per-command history.

    Used to isolate what the per-command KB layer adds over just fitting a
    single global distribution.  Point estimates mirror the KB's global
    fallback path exactly (global q50/q90), so in the cold-start scenario the
    global-only and KB latency predictions are identical by construction and
    any delta in the known-command scenario is purely the KB's per-command
    layer.
    """

    latency_p50_sec: float
    latency_p90_sec: float
    memory_p90_bytes: float
    cpu_p90_cores: float

    @classmethod
    def fit(cls, observations: list[ToolObservation]) -> "GlobalOnlyPredictor":
        estimator = LatencyEstimator(observations)
        # Predict for a synthetic unknown command so only the global path runs.
        probe = ToolObservation(
            execution_id="probe", tool_name="exec", command="__unknown__",
            command_digest="__unknown__", complete=True, exit_code=0, duration_sec=1.0,
        )
        latency = estimator.predict(probe)
        memory_values = [o.rss_peak_bytes for o in observations if o.rss_peak_bytes is not None]
        cpu_values = [o.cpu_utilization_avg_cores for o in observations if o.cpu_utilization_avg_cores is not None]
        memory_p90 = _p90(memory_values)
        cpu_p90 = _p90(cpu_values)
        return cls(
            latency_p50_sec=latency.p50_sec,
            latency_p90_sec=latency.p90_sec,
            memory_p90_bytes=memory_p90,
            cpu_p90_cores=cpu_p90,
        )

    def predict(self, observation: ToolObservation) -> Prediction:
        return Prediction(
            latency_p50_sec=self.latency_p50_sec,
            latency_p90_sec=self.latency_p90_sec,
            time_bucket=duration_bucket(self.latency_p90_sec),
            cpu_p90_cores=self.cpu_p90_cores,
            memory_p90_bytes=self.memory_p90_bytes,
            sample_count=0,
            match_level="global-default",
            confidence=0.0,
        )


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * 0.9
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class AblationScenario:
    """Per-predictor metrics for one eval scenario."""

    baseline: dict[str, float]
    global_only: dict[str, float]
    kb: dict[str, float]
    n_train: int
    n_eval: int

    def kb_mae_delta_pct(self) -> float:
        baseline_mae = self.baseline["mae_sec"]
        kb_mae = self.kb["mae_sec"]
        return round(((baseline_mae - kb_mae) / baseline_mae * 100.0) if baseline_mae > 0 else 0.0, 2)

    def kb_calibration_delta(self) -> float:
        return round(self.kb["calibration_p90"] - self.baseline["calibration_p90"], 4)


@dataclass(frozen=True)
class AblationResult:
    """Two-scenario ablation: cold-start + known-command.

    * ``cold_start`` — eval commands never seen in training (command-disjoint).
      KB and global-only both fall back to the global distribution, isolating
      the value of "learning from observations at all" over the fixed profile.
    * ``known_command`` — eval commands DO appear in training (within-command
      split).  KB uses per-command history, isolating the value of the KB layer
      over a single global distribution.
    """

    cold_start: AblationScenario
    known_command: AblationScenario
    n_total: int

    def summary(self) -> dict[str, object]:
        return {
            "n_total": self.n_total,
            "cold_start": {
                "n_train": self.cold_start.n_train,
                "n_eval": self.cold_start.n_eval,
                "baseline": self.cold_start.baseline,
                "global_only": self.cold_start.global_only,
                "kb": self.cold_start.kb,
                "kb_mae_delta_pct": self.cold_start.kb_mae_delta_pct(),
                "kb_calibration_delta": self.cold_start.kb_calibration_delta(),
            },
            "known_command": {
                "n_train": self.known_command.n_train,
                "n_eval": self.known_command.n_eval,
                "baseline": self.known_command.baseline,
                "global_only": self.known_command.global_only,
                "kb": self.known_command.kb,
                "kb_mae_delta_pct": self.known_command.kb_mae_delta_pct(),
                "kb_calibration_delta": self.known_command.kb_calibration_delta(),
            },
        }


def run_ablation(
    observations: list[ToolObservation],
    train_frac: float = 0.8,
    seed: int = 42,
    fixed_profile: FixedProfilePredictor | None = None,
) -> AblationResult:
    """Train each predictor on the same split and evaluate in both scenarios.

    The fixed baseline ignores training; the global-only predictor learns only
    the population distribution (no per-command history); the KB additionally
    keeps per-command histories.  Comparing them in the two scenarios isolates
    the contribution of each learning layer.  Shadow only — never changes real
    sizing.
    """
    from .dataset import command_disjoint_split, stratified_split
    from .kb import KnowledgeBaseBuilder

    baseline = fixed_profile or FixedProfilePredictor()

    def run_scenario(
        train: list[ToolObservation],
        eval_: list[ToolObservation],
    ) -> AblationScenario:
        global_only = GlobalOnlyPredictor.fit(train)
        builder = KnowledgeBaseBuilder()
        builder.add_many([obs for obs in train if obs.trusted])
        kb = builder.build()

        def metrics_for(predictor: Predictor) -> dict[str, float]:
            errors: list[float] = []
            within: list[bool] = []
            hits = 0
            total = 0
            overalloc: list[float] = []
            for observation in eval_:
                if observation.duration_sec is None:
                    continue
                prediction = predictor.predict(observation)
                errors.append(abs(prediction.latency_p50_sec - observation.duration_sec))
                within.append(observation.duration_sec <= prediction.latency_p90_sec)
                if observation.duration_sec > 0:
                    overalloc.append(
                        (prediction.latency_p90_sec - observation.duration_sec) / observation.duration_sec
                    )
                actual = observation.latency_bucket
                if actual is not None and prediction.time_bucket is not None:
                    hits += int(prediction.time_bucket == actual)
                    total += 1
            return {
                "mae_sec": round(sum(errors) / len(errors), 6) if errors else 0.0,
                "bucket_accuracy": round(hits / total, 4) if total else 0.0,
                "calibration_p90": round(sum(within) / len(within), 4) if within else 0.0,
                "mean_over_allocation_pct": round(sum(overalloc) / len(overalloc) * 100.0, 4)
                if overalloc
                else -1.0,
                "n_eval": float(len(eval_)),
            }

        return AblationScenario(
            baseline=metrics_for(baseline),
            global_only=metrics_for(global_only),
            kb=metrics_for(kb),
            n_train=len(train),
            n_eval=len(eval_),
        )

    cold_train, cold_eval = command_disjoint_split(observations, train_frac=train_frac, seed=seed)
    known_train, known_eval = stratified_split(observations, train_frac=train_frac, seed=seed)
    return AblationResult(
        cold_start=run_scenario(cold_train, cold_eval),
        known_command=run_scenario(known_train, known_eval),
        n_total=len(observations),
    )
