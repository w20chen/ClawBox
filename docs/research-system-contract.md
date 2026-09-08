# Research design and correctness rules

This document is for paper authors and developers changing ClawBox internals.
For normal configuration and execution, use the
[experiment guide](experiment-operations.md).

ClawBox studies whether many concurrent LLM agents can share physical memory
safely by using command-specific memory predictions and checkpointing virtual
machines while the model is producing a response.

## System structure

Each agent owns two CubeSandbox VMs:

- Runtime VM: runs OpenClaw and communicates with the model;
- Tool VM: owns the writable workspace and runs Tool commands over SSH.

CubeSandbox is the only VM platform. ClawBox decides when work may start and
when a VM may be checkpointed. It does not replace CubeSandbox networking or
execute Tool commands through an HTTP proxy.

The required command flow is:

```text
OpenClaw requests a Tool command
-> ClawTune identifies the command and predicts its resource use
-> ClawBox checks host memory and reserves capacity
-> restore the Tool VM if needed
-> resolve and verify the current SSH address
-> run the command over native SSH
-> collect cgroup-v2 and eBPF measurements
-> join records by session ID and execution ID
-> update training data when the experiment permits learning
```

One execution ID must be used from the memory decision through SSH completion
and telemetry collection. The SSH process starts only after the memory check
passes. Its reservation remains active until the process has exited and the
completion record has been collected. Retrying an admission request must not
start the command twice.

## ClawTune responsibilities

Reuse the sibling ClawTune repository for:

- command normalization and fallback keys;
- Tool duration, CPU, and memory statistics;
- P50 and P90 prediction;
- cgroup-v2 measurements;
- native eBPF/kprobe data;
- the Runtime command knowledge base.

ClawBox may validate, join, freeze, and report these records. It adds host
physical-memory control and VM checkpoint decisions. It should not contain a
second implementation of ClawTune's parser or percentile estimator.

For a normal comparison, create the prediction file from a separate recording
set, retain it with the experiment inputs, and reuse the exact file in every
variant. The test workload must not train its own predictor unless the variant
is explicitly studying online learning.

## Memory control

Before VM creation, VM restore, or a Tool command, ClawBox combines current
host memory use, existing reservations, the new reservation, and configured
headroom. It also checks a minimum host-free-memory limit shared by all
baselines. An underestimated command may trigger this safety limit but must not
be allowed to cause an unexplained host OOM.

Two memory measurements have different purposes:

- memory measured inside Tool VM describes one Tool command and trains
  command predictions;
- physical memory measured on the host describes VM density, checkpoint memory
  release, and memory use over time.

Do not substitute one for the other. P90 admission uses a calibration from
Tool measurements to the expected host-memory increase during execution.

## Checkpoint behavior

With `resident`, Runtime and Tool VMs remain running until the agent ends.

With `snapshot_pause`, ClawBox may checkpoint both VMs during a model request,
but only after the Tool has no active SSH process. ModelGateway keeps the
pending model response while Runtime is absent. Runtime is restored and checked
before that response is delivered. Tool may remain checkpointed until the next
Tool command.

When Tool is restored, ClawBox asks CubeSandbox for port `2222` again, advances
the endpoint generation, and verifies the Tool's SSH key and identity marker.
It does not assume that the old host and port remain valid.

The validated Kunpeng installation used local CubeSandbox commit `64102d9`,
based on `v0.7.0` with the same changes stored under `deploy/cubesandbox/`.
There, pause writes a memory snapshot and removes the running microVM while
keeping enough metadata to restore it. A 2026-09-06 diagnostic touched 1.5 GiB
inside one Tool VM, observed about 1.69 GiB host process memory, paused in about
0.93 seconds, and observed about 1.64 GiB return to host available memory. This
is a diagnostic example, not a paper comparison; formal results must use their
own raw host-memory samples.

## Evaluation rules

The c40 comparisons use recorded model responses and their original
timing. Everything else remains real: Runtime and Tool VMs, OpenClaw, SSH,
workspace changes, memory checks, checkpoint/restore, cgroup data, and eBPF
data. Any mismatch between replay input and the recorded request stops the run.

Use several representative coding traces, clean equivalent workspaces, a fixed
trace-to-agent assignment, and the same arrival schedule in every compared
variant. Validate identity and lifecycle at c1, then isolation at c4/c8, before
large runs. Small real-model c1/c2/c4 runs confirm that replay does not hide an
integration dependency.

Every final report separates real-model, managed replay, synthetic, and unit
test evidence. It reports throughput, agent completion time, Tool latency,
memory-wait time, host mean/peak memory, memory over time, prediction error,
fallback rate, checkpoint/restore cost, reclaimed memory, validation failures,
telemetry loss, wrong or duplicate Tool execution, OOM events, and VM leaks.

Unit tests, one successful SSH command, or one successful checkpoint are useful
checks but are not sufficient evidence for the complete research result.
