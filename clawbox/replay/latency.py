from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable

from .trace import ReplayAction


@dataclass(frozen=True, slots=True)
class LatencyObservation:
    input_chars: int
    output_chars: int
    latency_s: float


class LinearLatencyPredictor:
    """Small request-length model with no response-length oracle at decision time.

    Training fits latency against request characters. The median response size is
    retained only as a diagnostic and for future feature extensions; prediction
    never reads the current recorded response.
    """

    def __init__(self, *, intercept_s: float, seconds_per_input_char: float,
                 expected_output_chars: int, sample_count: int) -> None:
        self.intercept_s = max(0.0, float(intercept_s))
        self.seconds_per_input_char = max(0.0, float(seconds_per_input_char))
        self.expected_output_chars = max(0, int(expected_output_chars))
        self.sample_count = max(0, int(sample_count))

    @classmethod
    def fit(cls, observations: Iterable[LatencyObservation]) -> "LinearLatencyPredictor":
        rows = [row for row in observations if row.latency_s >= 0 and math.isfinite(row.latency_s)]
        if not rows:
            return cls(intercept_s=10.0, seconds_per_input_char=0.0005,
                       expected_output_chars=1024, sample_count=0)
        xs = [float(row.input_chars) for row in rows]
        ys = [float(row.latency_s) for row in rows]
        mean_x = statistics.fmean(xs)
        mean_y = statistics.fmean(ys)
        variance = sum((x - mean_x) ** 2 for x in xs)
        slope = 0.0 if variance == 0 else sum(
            (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
        ) / variance
        slope = max(0.0, slope)
        intercept = max(0.0, mean_y - slope * mean_x)
        return cls(
            intercept_s=intercept,
            seconds_per_input_char=slope,
            expected_output_chars=int(statistics.median(row.output_chars for row in rows)),
            sample_count=len(rows),
        )

    @classmethod
    def from_actions(cls, actions: Iterable[ReplayAction]) -> "LinearLatencyPredictor":
        return cls.fit(
            LatencyObservation(action.input_chars, action.output_chars, action.duration_s)
            for action in actions if action.kind == "llm"
        )

    def predict(self, action: ReplayAction) -> float:
        if action.kind != "llm":
            raise ValueError("latency prediction requires an LLM action")
        return max(0.0, self.intercept_s + self.seconds_per_input_char * action.input_chars)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "intercept_s": self.intercept_s,
            "seconds_per_input_char": self.seconds_per_input_char,
            "expected_output_chars": self.expected_output_chars,
            "sample_count": self.sample_count,
        }
