#!/usr/bin/env bash
set -u
pod=cube-node-t6pd8
sandbox_id=c07cac2eafbf471d8336afb6835d646c
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "find /data/log -maxdepth 4 -type f | grep -Ei 'CubeShim|cubeshim|serial' | tail -80"
echo matches
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "grep -R -l '$sandbox_id' /data/log 2>/dev/null | tail -30"
echo shim-log
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "grep -n -C 8 '$sandbox_id' /data/log/CubeShim/cube-shim-req.log | tail -160"
echo processes
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "ps -ef | grep -E '$sandbox_id|cube-shim|firecracker' | grep -v grep"
echo files
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "find /data/cubelet -path '*$sandbox_id*' -maxdepth 8 -print 2>/dev/null | head -100"
