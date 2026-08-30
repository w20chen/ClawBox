# P90 baseline implementation and replay validation, Kunpeng, 2026-08-30

## Implementation acceptance

- Host: ARM64 Kunpeng, 320 logical CPUs, four NUMA nodes.
- NUMA 0: CPUs `0-79`, 515,829 MiB local memory.
- Deployed image: `127.0.0.1:5000/clawbox/control-plane-arm64@sha256:b6113757b36f4fa3f1715b59c7609eeedc69163c0d52d8449a38ecbc621951fd`.
- The final controller and Tune-KB rollouts succeeded.
- The authenticated `/v1/kb/admission-prediction` endpoint returned HTTP 200 for an existing immutable generation.
- The complete test suite passed on both the Windows development machine and the real ARM64 host.

## Completed replay validation

This is a functional single-session validation, not the requested publishable
multi-trace concurrency sweep. It used one existing two-model-step trace,
`time_scale=1.0`, three randomized repetitions, NUMA 0, 2 GiB Runtime memory,
and 4 GiB Tool memory. Both arms produced the same final-state hash and no
failures.

| Arm | Tasks/min, mean (95% CI half-width) | Steps/min, mean (95% CI half-width) | Mean Firecracker RSS | Peak Firecracker RSS |
| --- | ---: | ---: | ---: | ---: |
| Fixed, resident | 1.3878 (0.0135) | 2.7757 (0.0271) | 857.8 MB | 1.190 GB |
| Fixed, LLM-wait checkpoint | 1.2891 (0.0170) | 2.5782 (0.0340) | 917.7 MB | 4.944 GB |

For this short-wait trace, checkpointing reduced task and step throughput by
7.1%. Each checkpoint run performed two Runtime+Tool cycles; mean total save
time was approximately 5.27 seconds and mean total restore time was 0.187
seconds. The high checkpoint peak is the transient memory cost while snapshot
files are written. This trace is too short to demonstrate idle-memory savings.

The complete machine-readable artifact is
`.artifacts/kunpeng-fixed-replay-study-summary-20260830.json` in the working
tree and `/data/openclaw-fixed-replay-eval-20260830/study-summary.json` on
Kunpeng.

## Why the full paper table was not fabricated

Two requirements could not be satisfied without additional user authority:

1. Kunpeng currently has one direct-Firecracker TAP pair. Creating enough pairs
   for concurrent sessions requires interactive `sudo`; non-interactive sudo
   was denied.
2. Only one replay-ready model trace exists. Recording repository-matched
   SWE-ReBench traces requires sending task content to the configured external
   model provider and incurring API cost. The environment required explicit
   approval for that transmission.

The available frozen P90 snapshot was also rejected for the 15Five workload:
it belongs to `clawbox/toolbridge-integration` and has only four coarse samples.
The runner now defaults to at least five samples and rejects repository
mismatches. Using that snapshot would make the predictive comparison invalid.

## Pre-registered full evaluation

Use `deploy/replay-suite.example.json` after the two permissions above are
available. The exclusive-CPU sweep is `[1, 8, 20, 40]`; each task owns one
Runtime and one Tool vCPU, so 40 tasks consume NUMA-0 CPUs `0-79`. The separate
memory-density sweep uses round-robin CPU placement and `[40, 60, 78]`; 78
fixed tasks configure 468 GiB of guest memory and retain a 32 GiB host reserve.
Use at least three repository-matched traces and three randomized repetitions.
Report per-workload curves and macro-averages, not pooled steps.
