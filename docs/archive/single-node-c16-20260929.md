# Historical 16-session, single-compute-node result (2026-09-29)

This result used NUMA 0 for running VMs and NUMA 1 for the shared memory
pool on one Kunpeng host. It predates the two-compute-node configuration.
For a new run, follow the [step-by-step guide](../self-service.md) and
[host configuration reference](../supernode.md).

Run ID: `eval-tiered-c16-r2-final6-20260929`. Each of the three policy
trials completed 16 of 16 sessions, passed task validation, recorded no
OOM kills or telemetry loss, linked all tool measurements to executions,
and verified VM cleanup.

## Configuration

- Workload: deterministic replay of `tobymao__sqlglot-4528`
- Offered concurrency: 16 sessions, each with a 2 GiB agent VM and a 4 GiB tool VM
- Total configured guest RAM: 96 GiB
- Compute-node low/high/capacity thresholds: 28/32/36 GiB
- Shared memory pool on NUMA 1: 128 GiB; at most 64 GiB reserved for running VMs
- Parent VM cgroup memory limit: 100 GiB, covering compute-node capacity plus the borrowing limit
- Arrival pattern: all sessions at once; one repetition
- Fixed per-command reservation: 259 MiB, calibrated from the training 90th percentile
- Predicted per-command reservations: 21.8867–225 MiB across 20 command entries
- Snapshot storage: memory-backed only, using incremental copy-on-write
- Agent VM image: `sha256:aefbc09b9d7b76a23284aa04a258cdd1a9057586742da2926bcd2922faf0dd40`
- Tool VM image: `sha256:98e10fcc9444e4d29cc4a154237800619396a9ef31ae43d1a81174fc84244deb`

The saved experiment and preflight files are the authority for these
identities if reproducing the result.

## Results

The admission-wait column sums waits across sessions and can exceed
the trial's elapsed time. Peak memory is measured on the compute node;
the host total also includes memory-backed snapshots.

| Policy | Trial duration | Sessions/min | Completion time P50/P95 | Total admission wait | Compute-node peak | Time above high watermark | Shared-pool use by running VMs | Checkpoints/restores |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed reservation (`tool-static-resident`) | 1757.29 s | 0.577 | 1656.01 / 1661.07 s | 13269.56 s | 32.10 GiB | 3.86 s / 0.32 GiB-s | 1 VM, 3.97 s | none |
| Predicted reservation (`tool-p50-resident`) | 1693.34 s | 0.600 | 1553.63 / 1587.85 s | 12651.03 s | 33.16 GiB | 284.41 s / 113.16 GiB-s | none | none |
| Predicted reservation and checkpointing (`tool-p50-wait-reactive`) | 1019.86 s | 1.035 | 898.11 / 922.08 s | 120.38 s | 28.68 GiB | 0 s / 0 GiB-s | none | 16 / 16 |

Relative to the fixed-reservation policy, the predicted-reservation
policy reduced trial duration by 3.6% and increased throughput by 3.9%.
Adding checkpointing reduced trial duration by 42.0% and increased
throughput by 79.3% relative to fixed reservations in this one run.

The mean reserved-to-observed extra-memory ratio fell from 19.85 with
fixed reservations to 3.70 with predictions. Completion time improved
only modestly because the running VMs' resident memory dominated under
pressure. The predicted-reservation trial spent longer above the high
watermark but stayed below configured capacity and completed without
an OOM; a sampled high-watermark crossing alone is not a failure.

Under the checkpointing policy, pressure selected VM pairs that were
waiting for model responses and had no active tool command. It
checkpointed eight pairs and restored them on demand. Total checkpoint
service time was 26.33 s; restore service time was 11.81 s. Peak
memory-backed checkpoint allocation was 48 GiB, and the combined
running-VM plus checkpoint peak in the shared pool was 48.46 GiB.
Measured compute-node use stayed below its high watermark, so this
trial did not need shared-pool memory for running VMs.

The whole-host peak increase was 71.19 GiB, higher than for the other
policies, because it includes memory-backed snapshots. Checkpointing
moves VM state from compute-node memory to the shared pool; it does
not make those pages disappear.

## Prediction evidence and limits

The predicted-reservation trials used frozen predictions for all
managed replay commands: 320 predicted command admissions and 512
fixed reservations for filesystem or other tool operations per trial.
The report's prediction-error and coverage fields are `n/a`
because it contains no paired prediction-versus-observation records.
This result supports the measured reservation and performance
comparisons above, but no claim about prediction mean absolute
error or exceedance rate.

There was one repetition, so the result does not estimate run-to-run
uncertainty. Its single-compute-node topology should not be directly
compared with a two-compute-node run.
