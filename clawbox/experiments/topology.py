"""Compute-node identity and deterministic session placement, separate from memory policy."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


def parse_cpu_list(value: str) -> set[int]:
    cpus: set[int] = set()
    for part in value.split(","):
        bounds = part.strip().split("-")
        if len(bounds) not in (1, 2) or any(not item.isdigit() for item in bounds):
            raise ValueError(f"invalid CPU list: {value}")
        first, last = int(bounds[0]), int(bounds[-1])
        if first > last:
            raise ValueError(f"invalid CPU range: {part}")
        cpus.update(range(first, last + 1))
    if not cpus:
        raise ValueError("CPU list must not be empty")
    return cpus


class ComputeNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    numa_node: int = Field(ge=0)
    cpus: str = Field(min_length=1)
    memory_capacity_mib: int = Field(gt=0)
    low_watermark_mib: int = Field(gt=0)
    high_watermark_mib: int = Field(gt=0)

    @model_validator(mode="after")
    def valid(self):
        parse_cpu_list(self.cpus)
        if not self.low_watermark_mib < self.high_watermark_mib < self.memory_capacity_mib:
            raise ValueError("compute node requires LOW < HIGH < local capacity")
        return self


def validate_compute_nodes(nodes: tuple[ComputeNode, ...], shared_node: int | None) -> None:
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ValueError("compute node IDs must be unique")
    if len({node.numa_node for node in nodes}) != len(nodes):
        raise ValueError("compute NUMA nodes must be distinct")
    assigned: set[int] = set()
    for node in nodes:
        if node.numa_node == shared_node:
            raise ValueError("shared memory NUMA node cannot be a compute node")
        cpus = parse_cpu_list(node.cpus)
        if cpus & assigned:
            raise ValueError("compute-node CPU sets must not overlap")
        assigned.update(cpus)


def place_session(nodes: tuple[ComputeNode, ...], index: int, explicit: tuple[str, ...] = ()) -> ComputeNode:
    """Keep Runtime and Tool on one home node; memory borrowing never changes CPU placement."""
    if index < 0 or not nodes:
        raise ValueError("placement requires compute nodes and a non-negative session index")
    if explicit:
        if index >= len(explicit):
            raise ValueError("session_compute_nodes does not cover this concurrency")
        return next(node for node in nodes if node.node_id == explicit[index])
    return nodes[index % len(nodes)]
