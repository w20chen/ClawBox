#!/usr/bin/env bash
set -u
pod=cube-node-t6pd8
sandbox_id=9d6b377e90d24c98821a04df80bddc80
port=20024

kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc '
  for m in ifindex_to_mvmmeta mvmip_to_ifindex remote_port_mapping local_port_mapping; do
    echo "MAP:$m"
    /usr/local/services/cubetoolbox/cube-vs/network/bin/cubevsmapdump "$m" 2>&1 || true
  done
'
echo LOGS
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc "grep -n -C 5 '$sandbox_id' /data/log/Cubelet/Cubelet-req.log | tail -120"
echo BPF
kubectl -n cube-system exec "$pod" -c cubelet -- sh -lc '
  echo DEV:eth0
  tc -s filter show dev eth0 ingress
  tc -s filter show dev eth0 egress
  echo DEV:cube-dev
  tc -s filter show dev cube-dev ingress
  tc -s filter show dev cube-dev egress
  echo DEV:z172.16.0.2
  tc -s filter show dev z172.16.0.2 ingress
  tc -s filter show dev z172.16.0.2 egress
'
echo TEST
if timeout 4 bash -lc "</dev/tcp/192.168.3.164/$port"; then echo connected; else echo refused; fi
