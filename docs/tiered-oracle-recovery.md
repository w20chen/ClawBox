# Recover and resume the tiered oracle study

This is a recovery runbook for the existing kunpeng deployment, not a claim that
the formal experiment has passed. The measured package remains incomplete.

## Host recovery prerequisite

The September 7 host journal records repeated soft lockups in mount-namespace
cleanup (`mntput_no_expire`) involving runc and a system service. Containerd
cannot reconcile exited containers, and systemd cannot create a recovery helper
scope. Ask the host administrator to recover the machine, using the console if
SSH cannot execute administrative commands. A reboot may be needed. Do not keep
force-deleting pods or rebuilding guest images to address this host fault.

Preserve the journal first if it has not already been downloaded:

```bash
ssh kunpeng
journalctl -k -b --no-pager > /home/weitianc/host-kernel-before-recovery.log
journalctl -u containerd -b --no-pager > /home/weitianc/containerd-before-recovery.log
```

The existing evidence directory is
`/home/weitianc/clawbox-tiered-study-20260907`. Its failed c1 attempts are
in `raw/`; retain them even after recovery. Do not overwrite an old attempt ID.

## Verify services and provenance after recovery

```bash
ssh kunpeng
uptime
journalctl -k -b --no-pager | grep -E 'soft lockup|hard LOCKUP|BUG:'
systemctl is-active containerd
kubectl -n cube-system get pods -o wide
kubectl -n cube-system get ds cube-node -o yaml
findmnt /data/cubelet/clawbox-tiered-20260907/warm
```

Absence of grep matches is normal. Confirm the node reaches 3/3 readiness and
does not accumulate new lifecycle errors. The desired Cubelet image is
`127.0.0.1:5000/clawbox/cubelet:tiered-094daaa`, registry digest
`sha256:977399a2e5dcca6659a2a8d4f356343d2352542bbf316358c623f1039324baa0`.
Read the actual pod image ID; an old annotation is not binary provenance.
Its build includes the existing HostPort hairpin patch and generated ARM64 BPF.
Runtime validation of this image is still required.

Preserve `/home/weitianc/ClawBox` and the existing CubeSandbox checkouts.
The experiment checkout is `/home/weitianc/ClawBox-tiered-2906a91`, initially
at `753de20`; the current single-c40 configuration milestone is `de9b155`.
Inspect `git status` before updating it. CubeSandbox source `094daaa` is in
`/home/weitianc/CubeSandbox-tiered-20260907-v4` and has no writable upstream.
A complete tracked-source archive was produced because its older Git history
has missing parents and `git bundle --all` failed.

WARM must be tmpfs with the stated 64 GiB bound, `mpol=bind:1`, and `noswap`.
A reboot may remove this mount: re-establish it only after checking the path
and existing contents. Validate actual page placement and writer cpusets.
Also verify LOCAL enforcement and COLD cache accounting; the configured NUMA
sampler alone does not prove a hard physical limit. Do not start formal runs
until the plan's capacity and placement gates pass.

## Reuse the validated build inputs

Runtime template: `tpl-f4c0c6a3c80a48be95b271b4`.
Tool template: `tpl-3ed0a8051a404e20a6d9890a`.
Guest kernel component: `sha256-5b59ed694175` (6.18.28 with the DAX fix).
Use template image digests from the checked-in experiment configuration.
Do not rebuild these merely because the host kernel is unhealthy.

```bash
cd /home/weitianc/ClawBox-tiered-2906a91
export PYTHONPATH=/home/weitianc/CubeSandbox-tiered-20260907-v4/sdk/python:.
export CUBE_API_URL=http://127.0.0.1:30030
export CUBE_PROXY_NODE_IP=127.0.0.1
export CUBE_PROXY_PORT_HTTP=30080
export CUBE_SANDBOX_DOMAIN=cube.local
export CLAWBOX_CONTROL_HOST=193.124.7.2
export CLAWBOX_OUTPUT_ROOT=/home/weitianc/clawbox-tiered-study-20260907/raw
export CLAWBOX_SANDBOX_CREATE_CONCURRENCY=4
sha256sum /home/weitianc/ClawBox/results/paper_replay_20260901_128g_v4/selected-traces/rec-a-enriched.jsonl
```

The trace hash must be
`12541145678c9f65c5b1388410f82a2d9954a26b100a3a781db5fa837eb83ae5`.
Use `.venv/bin/python -m pip`, not the `pip` entrypoint: the reused virtual
environment's pip launcher pointed to an older checkout. If installing dev
dependencies fails with missing SOCKS support, use the approved direct package
index with proxy variables unset, as tested during this recovery.

## Gates and formal run order

```bash
.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template tpl-f4c0c6a3c80a48be95b271b4 \
  --tool-template tpl-3ed0a8051a404e20a6d9890a \
  --node hostname-txyuq.foreman.pxe --control-host 193.124.7.2 \
  --count 1 --output /home/weitianc/clawbox-tiered-study-20260907/gates/tcp-c1-094daaa-after-recovery.json
```

Then pass the complete selected-rec-a c1 replay, both direct tier-transition
chains, and the c4 identity, overflow, accounting, and correctness checks.
The historical successful small smoke trace is not a substitute for rec-a.
The production Tool bridge must start before testing native SSH, and the
Runtime must reach the Tool through the semantic TCP endpoint with identity
validation. `169.254.68.6` is an expected guest inner address.

Only after these gates pass, execute
`examples/experiments/tiered-oracle-rec-a-c40.yaml`: 13 policies, one run each,
no c8/c60 or repeated formal runs. Retain admission timeouts and other genuine
policy failures. Before starting, freeze the run manifest, physical resource
protocol, cache protocol, and measured storage/time estimate. These steps are
still outstanding; this runbook intentionally does not label an unvalidated
command sequence as a completed experiment.

## Download evidence

Archive on Linux before downloading so long Runtime trace names survive:

```bash
tar -czf /home/weitianc/clawbox-tiered-final-evidence.tar.gz \
  -C /home/weitianc clawbox-tiered-study-20260907
sha256sum /home/weitianc/clawbox-tiered-final-evidence.tar.gz
```

From the local workspace, use SCP and `Get-FileHash -Algorithm SHA256` to
verify the archive. Preserve original recording/resource evidence, prediction
files, source archives, and failed attempts along with successful runs.
Keep raw request/deployment logs out of public Git; commit the analysis,
redacted provenance, checksums, tables, plots, and the supported conclusions.
