#!/usr/bin/env bash
# Print M1 smoke status to a file (avoid docker output streaming over SSH pty,
# which wedges the persistent PowerShell terminal).
OUT=/tmp/m1-status.txt
: > "$OUT"
{
  echo "== containers =="
  docker ps --format '{{.Names}} {{.Status}}'
  echo "== api healthz =="
  curl -s http://127.0.0.1:8085/healthz; echo
  echo "== dispatcher logs =="
  docker logs clawbox-m1-dispatcher 2>&1 | tail -10
  echo "== api logs =="
  docker logs clawbox-m1-api 2>&1 | tail -10
  echo "== CRs =="
  kubectl get sandboxtask -n clawbox-benchmarks 2>/dev/null | grep 'run-' || echo "(no run- CRs yet)"
  echo "== status done =="
} > "$OUT" 2>&1
echo written
