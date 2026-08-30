"""Small-sample descriptive statistics for replay experiments."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable


_TWO_SIDED_T_95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)


def summary_stats(values: Iterable[float]) -> dict[str, float | int | str | None]:
    """Mean, sample deviation, and two-sided 95% Student-t half-width."""
    samples = [float(value) for value in values]
    if not samples:
        return {
            "n": 0, "mean": None, "stdev": None, "ci95_half_width": None,
            "ci95_method": "student-t", "degrees_of_freedom": None,
        }
    if len(samples) == 1:
        return {
            "n": 1, "mean": samples[0], "stdev": None, "ci95_half_width": None,
            "ci95_method": "not-estimable", "degrees_of_freedom": 0,
        }
    deviation = statistics.stdev(samples)
    degrees = len(samples) - 1
    critical = _TWO_SIDED_T_95[degrees - 1] if degrees <= 30 else 1.96
    return {
        "n": len(samples), "mean": statistics.fmean(samples), "stdev": deviation,
        "ci95_half_width": critical * deviation / math.sqrt(len(samples)),
        "ci95_method": "student-t", "degrees_of_freedom": degrees,
    }
