#!/usr/bin/env bash
set -u
ip -br addr
echo routes
ip route
echo local-tests
for host in 127.0.0.1 192.168.3.164 193.124.7.2; do
  printf '%s ' "$host"
  timeout 3 nc -vz "$host" 20017 2>&1 || true
done
echo pod-test
kubectl -n cube-system exec cube-api-67477b67f7-9d74j -- sh -lc 'timeout 3 sh -c "</dev/tcp/192.168.3.164/20017"' 2>&1 || true
