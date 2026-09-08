#!/usr/bin/env bash
set -eu
study=/home/weitianc/clawbox-tiered-study-20260907
mkdir -p "$study/provenance/recovery-20260907"
journalctl -u containerd -n 300 --no-pager > "$study/provenance/recovery-20260907/containerd.log" 2>&1
timeout 30s kubectl -n cube-system get events -o json > "$study/provenance/recovery-20260907/events.json" 2>&1 || true
timeout 30s kubectl -n cube-system get pods -o wide > "$study/provenance/recovery-20260907/pods.txt" 2>&1 || true
tar -czf /home/weitianc/clawbox-tiered-evidence-20260907-recovery.tar.gz -C /home/weitianc clawbox-tiered-study-20260907
sha256sum /home/weitianc/clawbox-tiered-evidence-20260907-recovery.tar.gz
