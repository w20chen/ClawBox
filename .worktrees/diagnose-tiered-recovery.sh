#!/usr/bin/env bash
set -u
date -Is
uptime
timeout 20s kubectl -n cube-system describe pod cube-node-554rc
timeout 20s kubectl -n cube-system logs cube-node-554rc -c init --tail=60
timeout 20s kubectl -n cube-system get events --sort-by=.lastTimestamp | tail -30
timeout 10s ps -eo pid,ppid,stat,comm,wchan:32 --sort=stat | tail -60
timeout 10s df -h /data
timeout 10s kubectl get nodes
