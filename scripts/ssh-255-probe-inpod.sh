#!/usr/bin/env bash
# CBX-M0-007: layered SSH-255 probe, IN-POD variant.
#
# Runs inside the Runtime Pod (via `kubectl exec`) to measure the REAL
# runtime->tool SSH path: the Tool-ingress NetworkPolicy is fail-closed to the
# runtime pod, so host-side probing only ever sees blocked handshakes. The pod
# already holds the client key (/var/run/secrets/tool-ssh/id_ed25519) and the
# known_hosts written by runtime-entrypoint (/state/<TASK>/ssh/known_hosts).
#
# usage: ssh-255-probe-inpod.sh TASK_ID [COUNT]
#   run with: kubectl exec -n <ns> <runtime-pod> -- bash -s < TASK_ID <COUNT>
set -euo pipefail

TASK="${1:?usage: ssh-255-probe-inpod.sh TASK_ID [COUNT]}"
COUNT="${2:-1000}"
KEY="/var/run/secrets/tool-ssh/id_ed25519"
KNOWN="/state/${TASK}/ssh/known_hosts"
TARGET="executor@${TASK}-tool"
PORT=2222

declare -A counts
declare -A samples
for layer in ok dns tcp ssh-handshake request post-command-255 command-exit; do
  counts[$layer]=0
done

classify() {
  local rc="$1" out="$2"
  if [[ "${rc}" -ne 0 ]] && grep -q 'probe-ok' <<<"${out}"; then
    echo "post-command-255"
  elif grep -qiE 'Could not resolve|Temporary failure in name|Name or service not known' <<<"${out}"; then
    echo "dns"
  elif grep -qiE 'Connection refused|Connection timed out|No route to host|Network is unreachable' <<<"${out}"; then
    echo "tcp"
  elif grep -qiE 'kex_exchange_identification|Connection closed by|Host key verification|REMOTE HOST IDENTIFICATION|Permission denied|Connection reset|ssh_exchange_identification' <<<"${out}"; then
    echo "ssh-handshake"
  elif grep -qiE 'session request failed|mux_client_request_session|channel.*open failed|Exec request failed' <<<"${out}"; then
    echo "request"
  elif [[ "${rc}" -ne 0 ]]; then
    echo "command-exit"
  else
    echo "ok"
  fi
}

echo "== in-pod ssh-255 probe task=${TASK} count=${COUNT} target=${TARGET} =="
for ((i = 1; i <= COUNT; i++)); do
  rc=0
  out="$(timeout -k 5 15 ssh -p "${PORT}" -i "${KEY}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN}" "${TARGET}" 'echo probe-ok' 2>&1)" || rc=$?
  layer="$(classify "${rc}" "${out}")"
  counts[$layer]=$((counts[$layer] + 1))
  if [[ -z "${samples[$layer]:-}" && "${layer}" != ok ]]; then
    samples[$layer]="$(printf '%s' "${out}" | grep -iE 'Connection (closed|reset|refused|timed)|kex|Permission denied|session request|Could not resolve|Name or service' | head -1)"
  fi
  if (( i % 100 == 0 )); then
    echo "  ${i}/${COUNT}: ok=${counts[ok]} dns=${counts[dns]} tcp=${counts[tcp]} hs=${counts[ssh-handshake]} req=${counts[request]} post255=${counts[post-command-255]} cmd=${counts[command-exit]}"
  fi
done
echo "== results =="
for layer in ok dns tcp ssh-handshake request post-command-255 command-exit; do
  printf '%-18s %d\n' "${layer}" "${counts[$layer]}"
done
echo "== failure samples =="
for layer in dns tcp ssh-handshake request post-command-255 command-exit; do
  [[ -n "${samples[$layer]:-}" ]] && printf '%-18s %s\n' "${layer}" "${samples[$layer]}"
done
