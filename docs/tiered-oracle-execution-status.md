# Tiered oracle execution record

## Latest authorized scope

The user's later instructions supersede the original matrix sizes and repetitions:
complete c1 replay and c1/c4 correctness and tier-transition gates, then run all
13 canonical policies at c40 once. Do not run c8 or c60 or repeated formal runs.
Retain the selected rec-a recording, its responses and timing, and revised RQ2.
Download all traces and logs. Commit and push each milestone.

The main configuration is `examples/experiments/tiered-oracle-rec-a-c40.yaml`.
It offers 240 GiB of VM capacity (40 pairs, Runtime 2 GiB and Tool 4 GiB),
against 64 GiB LOCAL and 64 GiB WARM. These are configuration values, not
claims of measured consumption or validated physical enforcement. The existing
resource and placement gates remain mandatory before formal use. One run per
policy supports a descriptive case study; it cannot estimate between-run variance.
The original plan's sensitivities and capacity-matched controls remain unmeasured
unless separately executed; conclusions requiring them must be marked unsupported.

## Recovery checkpoint, 2026-09-07

ClawBox source before this update: `753de20`, pushed to its existing upstream.
Isolated remote checkout: `/home/weitianc/ClawBox-tiered-2906a91`.
CubeSandbox source: `/home/weitianc/CubeSandbox-tiered-20260907-v4`.

The CubeSandbox branch reached `094daaa` after applying the existing
`deploy/cubesandbox/hostport-hairpin.patch`. Its parent tiered implementation
had omitted this patch. The image build compiled the ARM64 BPF programs and
Cubelet; functional forwarding validation is still outstanding.

Image: `127.0.0.1:5000/clawbox/cubelet:tiered-094daaa`.
Registry digest: `sha256:977399a2e5dcca6659a2a8d4f356343d2352542bbf316358c623f1039324baa0`.
The image update was submitted, but successful node startup has not been verified.
At the latest probe, `cube-node-554rc` was `Init:0/1`, with zero of three
containers ready. SSH authentication succeeds; a simple command took about
37 seconds in the authenticated session. The cause is not established.

Complete selected-rec-a c1 replay has not passed. The historical successful c1
used a different, small smoke trace and cannot satisfy that gate. No formal
c40 results are available at this checkpoint. Existing failed attempts remain
under `/home/weitianc/clawbox-tiered-study-20260907/raw/`.

Guest address `169.254.68.6` is CubeSandbox's normal inner address, not evidence
of stale snapshot networking. Tool bridge startup and Runtime-to-Tool identity
checks must both succeed before interpreting endpoint connectivity results.
