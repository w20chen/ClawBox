# c320 supplement audit bundle

This directory preserves the raw source data for the five supplementary runs
executed on `kunpeng` on 2026-10-18. The archive is a byte-for-byte copy of the
five terminal result directories; it has not been filtered to retain only
favorable sessions or events.

## Files

- `supplement-audit-20261018.tar.zst`: complete raw archive, 43,586,735 bytes
  compressed and approximately 1.30 GiB extracted.
- `ARCHIVE-SHA256.txt`: checksum of the compressed archive.
- `supplement-audit-20261018-files.sha256`: SHA-256 for all 1,431 source files,
  with paths relative to the extracted archive root.
- `metadata/`: convenient uncompressed copies of every run's exact
  `experiment.yaml`, `qualification.json`, `run-state.json`, arm result, and
  generated summary. Raw attempts are kept in the archive to avoid expanding
  another 1.30 GiB in the repository workspace.
- `analyze-cpu.py`: read-only event-stream analyzer for aggregate VM cgroup CPU,
  physical-node CPU utilization, resident VM count, Tool duration, and cgroup
  throttling. Use `--seconds` to reproduce a frozen observation window rather
  than including finalization and cleanup events.

Archive SHA-256:

```text
53c1cd1822d70516b9305722ecac133ab9c6eb1a0cb051bf05ad746cdf2b8533
```

## Included runs

| Run ID | Attempt | Arm ID | Role | Terminal state |
|---|---|---|---|---|
| `supp-m320-full-20261018-01` | `attempt-495577f4257d45ea` | `8d0383797b333e2bc5fa85df` | Planned Full | Failed on arm timeout; cleanup verified |
| `supp-m320-aoff-20261018-01` | `attempt-dd497f44c23346ad` | `12d6758dbaf21dc4dfba56ca` | Matched A-off | Failed on arm timeout; cleanup verified |
| `supp-m320-tuned-full-20261018-01` | `attempt-556fcda1418d4f97` | `9f43842632d180795bdaa10f` | Relaxed-memory/gate-12 Full calibration | Deliberately cancelled; cleanup verified |
| `supp-m320-paced-full-20261018-01` | `attempt-cfd476cc16d24137` | `dea29fe56af150ab30a377a0` | 1.5-second-arrival Full calibration | Deliberately cancelled; cleanup verified |
| `supp-m320-sustainable-full-20261018-01` | `attempt-652679496d194ce9` | `af071069ac29418302d0cb5c` | 2.5-second-arrival Full calibration | Deliberately cancelled; cleanup verified |

The two timeout runs are the planned comparison. The other three runs are
configuration diagnostics and are not silently relabeled as formal ablations.

## Raw-data layout

For each run:

- `<run>/experiment.yaml` is the exact resolved configuration executed by the
  public experiment workflow.
- `<run>/qualification.json` records pre-run qualification.
- `<run>/run-state.json` records attempts, terminal state, artifact roots, and
  `cleanup_verified`.
- `<run>/attempts/<attempt>/<arm>/events/<arm>.jsonl` is the authoritative,
  ordered event stream used for the frozen-window metrics.
- `<run>/attempts/<attempt>/<arm>/model-gateway/<session>.json` contains the
  per-session replay transcript and progress state.
- Other files below the attempt directory contain worker, lifecycle, cleanup,
  and per-session logs.
- `<run>/arms/<arm>.json` and `summary.*` exist for the two runs that reached
  their configured timeout. The cancelled calibration runs intentionally have
  no completed arm result; their evidence remains in `events/*.jsonl` and
  `run-state.json`.

The separately retained frozen-window JSON files are in
`../supplement-run-20261018/`. They were produced by
`../summarize-window.py`; unfinished requests remain censored and Tool
completions are not counted as successful Agents.

## Metric-to-event mapping

| Reported item | Source events or field |
|---|---|
| Offered sessions | fixed arrival schedule in `experiment.yaml`, truncated at the observation window |
| VM-created sessions | distinct `session_id` values in `sandbox_created` |
| Successful Agents | `session_complete` with `valid: true` |
| Failed Agents | `session_failed` |
| Agent/maintenance progress | `tool_admitted.execution_scope` joined to `tool_completed.execution_id` |
| Admission latency | `tool_admitted.admission_blocked_seconds` |
| LOCAL/shared/WARM peaks | fields in `memory_sample` |
| OOM and PSI | first-to-last cgroup counters and sampled cgroup PSI in `memory_sample` |
| Borrow/checkpoint/restore actions | `shared_memory_borrowed`, `sandbox_paused`, and `sandbox_restored` |
| Cleanup status | `run-state.json` arm field `cleanup_verified` |

## Verification and extraction

From PowerShell in this directory:

```powershell
Get-FileHash -Algorithm SHA256 .\supplement-audit-20261018.tar.zst
New-Item -ItemType Directory -Force .\extracted | Out-Null
tar -xf .\supplement-audit-20261018.tar.zst -C .\extracted
```

On a system with GNU `sha256sum`, verify every extracted source file from the
`extracted` directory:

```bash
sha256sum -c ../supplement-audit-20261018-files.sha256
```

The audit interpretation and frozen-window result table are in
`../SUPPLEMENT-RESULTS-20261018.md`.

For example, reproduce the planned Full CPU evidence without extracting the
archive:

```powershell
tar -xOf .\supplement-audit-20261018.tar.zst `
  supp-m320-full-20261018-01/attempts/attempt-495577f4257d45ea/8d0383797b333e2bc5fa85df/events/8d0383797b333e2bc5fa85df.jsonl |
  python .\analyze-cpu.py --label full --seconds 960
```
