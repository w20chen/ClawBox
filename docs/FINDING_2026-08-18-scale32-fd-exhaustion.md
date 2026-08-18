# Finding: Kata shim FD exhaustion caps concurrent cells at ~20 (scale 32)

> Date: 2026-08-18 · Target: hostname-txyuq.foreman.pxe (openEuler aarch64, K8s
> 1.35.7, Kata 3.31.0, Firecracker 1.12.1) · Reproducer: `single-image-scale.sh --count 32`

## Summary

The M0-008 scale ladder passed 1/2/4/8/16 with **zero platform failures and zero
leaks** (32 VMs at count=16, devmapper Data/Meta% barely moved, all cells
`Cleaned/Succeeded`, post-run `firecracker/jailer/kata-shim` counts = 0).

At **count=32** the run partially failed: **20/32 succeeded, 12/32 failed**
(cells 020–031) with a platform-side error during Kata container creation.

## Root cause

Failed tool/runtime pods reported:

```
Error: failed to create containerd task: failed to create shim task: Others(
  "failed to handle message create container
  Caused by:
    0: handler volumes
    1: new share fs volume Mount { destination: "/var/run/secrets/tool-ssh", ... }
    2: No file descriptors available (os error 24)
```

`os error 24` = EMFILE. Containerd itself runs with `LimitNOFILE ≈ 1e9`
(verified via `/proc/<containerd>/limits`), but **the Kata shim resets its own
soft RLIMIT_NOFILE to 1024** (hard 524288, verified via `/proc/<shim>/limits`).
During the admission burst at ~20 concurrent cells, a shim exceeds 1024 open FDs
while creating the share-fs volume for the Secret bind mount → EMFILE → the
container never starts → the cell job fails → `Cleaned/Failed`.

Not memory (1965 GiB free during the run), not disk (484 GiB free), not
containerd errors (clean journal in the window), not the thin pool (Data 0.47% /
Meta 0.33%).

## Evidence

- `kubectl get events -n clawbox-benchmarks` (warning `Failed` with the EMFILE
  stack for cells 020–031, tool and runtime pods)
- `/proc/<shim>/limits`: `Max open files 1024 524288 files`
- `/proc/<containerd>/limits`: `Max open files 1073741816 1073741816 files`
- Cleanup after run: 0 firecracker / 0 jailer / 0 kata shims / 0 pods (no leak)

## Impact / qualification

- Highest *qualified* concurrent concurrency on this host+kata config is
  **≤ 19 cells** (cells 001–019 succeeded; failures began at the 20th). With an
  N+1 margin that suggests reserving ≈ 19 cells as the current ceiling until
  the FD limit is raised.
- This is exactly the class of capacity cliff M0-008 exists to find; 8 and 16
  passed cleanly but 32 did not.

## Fix direction (not yet implemented)

1. Make the Kata shim raise `RLIMIT_NOFILE` (soft) above 1024 — either patch
   kata's runtime-rs shim launch, or find/set a Kata config knob if one exists;
   verify the resulting `/proc/<shim>/limits` on a fresh sandbox.
2. Re-run count=32 with the same unique-prefix ladder and confirm 32/32.
3. Track in the roadmap under M6 capacity / multi-node work; do not claim 32+
   concurrency until this is resolved and re-evidenced.

## Diagnostic helper

`scripts/diag-fd.sh` prints system/containerd/shim FD limits and per-shim open
FD counts; scp it to the target and run `bash /tmp/diag-fd.sh`.
