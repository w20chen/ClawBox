"""Auditable cost comparison for joint NUMA borrowing and WARM eviction."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


GIB = 1024**3


@dataclass(frozen=True, slots=True)
class MemoryActionEstimate:
    action: str
    configured_bytes: int
    estimated_seconds: float
    immediate_seconds: float
    runtime_penalty_seconds: float
    expected_restore_seconds: float
    snapshot_tier: str | None = None
    basis: str = "configured_vm_gib"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryActionCostModel:
    """Compare configured empirical rates without inventing hardware costs."""

    name = "configured_empirical_action_cost_v1"

    def __init__(self, costs: Any) -> None:
        self.costs = costs

    def borrow(self, configured_bytes: int) -> MemoryActionEstimate:
        configured_bytes = self._valid_size(configured_bytes)
        gib = configured_bytes / GIB
        immediate = gib * self.costs.live_borrow_service_seconds_per_gib
        runtime_penalty = (
            gib * self.costs.live_borrow_runtime_penalty_seconds_per_gib
        )
        return MemoryActionEstimate(
            action="borrow", configured_bytes=configured_bytes,
            estimated_seconds=immediate + runtime_penalty,
            immediate_seconds=immediate,
            runtime_penalty_seconds=runtime_penalty,
            expected_restore_seconds=0.0,
        )

    def reclaim(self, configured_bytes: int, *, snapshot_tier: str = "warm"
                ) -> MemoryActionEstimate:
        configured_bytes = self._valid_size(configured_bytes)
        gib = configured_bytes / GIB
        if snapshot_tier == "warm":
            checkpoint_rate = self.costs.warm_checkpoint_seconds_per_gib
            restore_rate = self.costs.warm_restore_seconds_per_gib
        elif snapshot_tier == "cold":
            checkpoint_rate = self.costs.cold_checkpoint_seconds_per_gib
            restore_rate = self.costs.cold_restore_seconds_per_gib
            if checkpoint_rate is None or restore_rate is None:
                raise ValueError("COLD action cost is not calibrated")
        else:
            raise ValueError(f"invalid snapshot_tier: {snapshot_tier}")
        immediate = gib * checkpoint_rate
        restore = (
            gib * restore_rate
            * self.costs.expected_restore_probability
        )
        return MemoryActionEstimate(
            action="reclaim", configured_bytes=configured_bytes,
            estimated_seconds=immediate + restore,
            immediate_seconds=immediate,
            runtime_penalty_seconds=0.0,
            expected_restore_seconds=restore,
            snapshot_tier=snapshot_tier,
        )

    def choose(self, *, borrow_bytes: int | None,
               reclaim_bytes: int | None,
               reclaim_tier: str = "warm",
               ) -> tuple[str | None, dict[str, dict[str, Any]]]:
        estimates: dict[str, MemoryActionEstimate] = {}
        if borrow_bytes is not None and borrow_bytes > 0:
            estimates["borrow"] = self.borrow(borrow_bytes)
        if reclaim_bytes is not None and reclaim_bytes > 0:
            estimates["reclaim"] = self.reclaim(
                reclaim_bytes, snapshot_tier=reclaim_tier,
            )
        if not estimates:
            return None, {}
        # A tie retains the safer existing preference for an idle WARM victim.
        chosen = min(
            estimates.values(),
            key=lambda item: (item.estimated_seconds, item.action != "reclaim"),
        ).action
        return chosen, {key: value.as_dict() for key, value in estimates.items()}

    @staticmethod
    def _valid_size(configured_bytes: int) -> int:
        if isinstance(configured_bytes, bool) or configured_bytes <= 0:
            raise ValueError("configured_bytes must be positive")
        return int(configured_bytes)

    @property
    def provenance(self) -> str:
        return str(self.costs.provenance)
