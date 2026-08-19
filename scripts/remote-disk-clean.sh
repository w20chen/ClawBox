#!/usr/bin/env bash
# Free root-fs disk space on the Kunpeng test node so kubelet lifts the
# disk-pressure eviction taint. Run detached on the target (setsid + nohup).
set -u
LOG=/tmp/disk-clean.log
{
  echo "== start $(date) =="
  echo "== before =="
  df -h / | tail -1

  echo "== docker image prune -a =="
  docker image prune -a -f

  echo "== remove swerebench x86_64 eval images (leftovers) =="
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'swerebench/sweb.eval.x86_64' || true); do
    echo "rm $img"
    docker image rm -f "$img" || true
  done

  echo "== remove build cache =="
  docker builder prune -a -f || true

  echo "== after =="
  df -h / | tail -1
  echo "== done $(date) =="
} >> "$LOG" 2>&1
