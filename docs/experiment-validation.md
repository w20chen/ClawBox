# Unresolved experiment validation

- [ ] Verify dense-run cleanup after interruption within the supervisor deadline;
  an HTTP timeout bounds a request, not a serial cleanup of hundreds of VMs.
- [ ] Verify high-density create latency and shared-memory rebind progress after
  the SDK, cpuset no-op, and lifecycle observation fixes. Unit tests do not
  establish that kernel migration stalls or service-side HTTP 408s are eliminated.

- [ ] Quantify and reduce the high-concurrency admission/control-path overhead
  before attributing Full-320/640/1280 throughput solely to CPU, memory, or Tool
  execution contention. Current evidence shows multi-second per-operation
  admission and completion handling, frequent backend-maintenance requests, and
  `execution_already_inflight` serialization. Preserve existing measurements as
  implementation-inclusive results; do not present them as isolated scheduling
  capacity. Required closure evidence: a before/after raw-event breakdown with
  unchanged workload, arrival sequence, VM resources, and mechanisms.
