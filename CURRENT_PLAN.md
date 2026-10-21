# Current Plan

Updated: 2026-10-21

Prepare **500+ distinct, validated ARM64 SWE-ReBench trace environments** for high-density CubeSandbox experiments. Concurrency is independently configured: assign each VM pair `pair_index % valid_trace_count`, with isolated writable environments.

- Recover prepared `/testbed` filesystems from official benchmark images; do not reconstruct repositories through cloning/resetting.
- Preserve recorded source and dependency versions when adapting to ARM64. Share identical base/dependency layers. Skip genuinely unportable cases only while retaining 500+ valid traces. Cross-architecture equivalence requires validation; it is not bit-identical reproduction.

## Background work

- Host: `weitianc@193.124.7.2` (Kunpeng).
- Run directory: `/home/weitianc/clawbox-swe-images-20261021`.
- Service: `clawbox-swe-prepare-20261021.service`, enabled with automatic restart.
- Monitor: `build/progress.json`, `logs/`, and `HANDOFF.txt` in the run directory.
- Last verified snapshot: 561 official filesystems recovered, 408 native builds, 166 environments passed dependency/source/SSH checks. These are intermediate counts, **not successful Cube replay counts**.
- Direct registry mirrors and a checksum-verified package cache are primary; desktop VPN is fallback. Approved old-pool cleanup and free-space guards protect remaining disk capacity.

## Remaining work

1. Finish shared base variants and task builds; obtain 500+ environments passing source, dependency, and ARM runtime checks.
2. Complete Cube image/template registration, real sandbox checks, and recorded replay/telemetry validation.
3. Publish a validated trace-to-image/template manifest; verify modulo assignment and isolation at the chosen concurrency.

Interactive goal work is paused; background jobs continue. The overall goal remains incomplete.
