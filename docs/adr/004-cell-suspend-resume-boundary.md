# ADR-004: Cell suspend/resume lifecycle boundary

Status: proposed interface, implementation deferred

## Context

LLM inference leaves the Runtime VM idle for material periods. A future
high-density scheduler may freeze or checkpoint a Cell while an LLM request is
in flight, then resume it when the response arrives. The current API supports
only `Running -> Cancelled`; cancellation is irreversible, while suspension is
reversible and must not be modeled as a terminal outcome.

## Decision

The current controller exposes two boundaries that suspend/resume must reuse:

1. `allow_workload_start` gates physical Pod/Job materialization. A future
   resume operation consumes the same per-cycle start budget as initial VM
   creation, preventing a response burst from becoming a devmapper start burst.
2. `capacity_reservation_for_phase` is the sole mapping from lifecycle phase to
   charged resources. A memory-preserving pause keeps the full reservation. A
   checkpoint-and-delete implementation may return a reduced reservation (for
   example storage only) until resume.

`Cleaned` is a stable final phase, separate from result-terminal phases that
still require cleanup. Future phases such as `Suspending`, `Suspended`, and
`Resuming` are non-terminal and must be classified explicitly.

The API change belongs in a new served CRD version. It may extend desired state
with reversible `Running <-> Suspended` transitions and irreversible
`Running|Suspended -> Cancelled`; it must not weaken the existing v1alpha1/2 CEL
immutability rules in place. The implementation must also record suspended
duration so task execution budgets exclude provider wait time while retaining a
separate wall-clock safety deadline.

## Non-goals of this change

- No VM freeze/checkpoint mechanism is selected here.
- No CPU or memory is overcommitted based only on an application-level idle
  signal.
- Network isolation, task-scoped credentials, artifact durability, and finalizer
  cleanup remain mandatory across suspend and resume.
