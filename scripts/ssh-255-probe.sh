#!/usr/bin/env bash
# CBX-M0-007: layered SSH-255 probe.
#
# The runtime->tool SSH occasionally fails with exit 255. This probe runs N
# short `exec` commands against a live Tool Bridge and classifies every failure
# into one of five layers (DNS / TCP / SSH handshake / request / command exit)
# so the frequency and root-cause distribution can be quantified.
#
# It runs from a box with kubectl access to the target cluster (operator host),
# using the cell's own auth Secret (the runtime's client key + tool host key).
# The key is never written to evidence or logs outside the probe.
#
# usage: ssh-255-probe.sh --cell NAME [--namespace NS] [--count N]
#   --cell       live cell whose Tool Pod will be probed (must be ToolReady)
#   --namespace  namespace of the cell (default clawbox-benchmarks)
#   --count      number of short execs (default 1000)
#   --keep       keep the temp key dir instead of deleting it
set -euo pipefail

CELL=""
NAMESPACE="clawbox-benchmarks"
COUNT=1000
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cell) CELL="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --count) COUNT="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done
[[ -n "${CELL}" ]] || { echo "--cell is required" >&2; exit 64; }
[[ "${COUNT}" =~ ^[0-9]+$ ]] || { echo "--count must be a positive integer" >&2; exit 64; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 69; }
command -v ssh >/dev/null || { echo "ssh is required" >&2; exit 69; }

key_dir="$(mktemp -d)"
cleanup() { [[ "${KEEP}" == 1 ]] || rm -rf "${key_dir}"; }
trap cleanup EXIT

# Resolve the Tool Pod IP and the auth material from the cell's own Secret.
pod_ip="$(kubectl -n "${NAMESPACE}" get pod "${CELL}-tool" \
  -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
[[ -n "${pod_ip}" ]] || { echo "tool pod ${CELL}-tool has no IP (is the cell alive?)" >&2; exit 66; }
tool_port=2222

kubectl -n "${NAMESPACE}" get secret "${CELL}-auth" -o jsonpath='{.data.id_ed25519}' \
  | base64 -d >"${key_dir}/id_ed25519"
chmod 0600 "${key_dir}/id_ed25519"
host_pub="$(kubectl -n "${NAMESPACE}" get secret "${CELL}-auth" \
  -o jsonpath='{.data.ssh_host_ed25519_key.pub}' | base64 -d)"
printf '[%s]:%s %s\n' "${pod_ip}" "${tool_port}" "${host_pub}" >"${key_dir}/known_hosts"

classify() {
  # $1 = ssh exit code, $2 = ssh combined stderr
  local rc="$1" out="$2"
  # The flaky symptom: the remote command clearly ran (expected output seen)
  # but the ssh client still returned non-zero (e.g. 255 on channel close).
  if [[ "${rc}" -ne 0 ]] && grep -q 'probe-ok' <<<"${out}"; then
    echo "post-command-255"
  elif grep -qiE 'Could not resolve hostname|Temporary failure in name resolution|Name or service not known' <<<"${out}"; then
    echo "dns"
  elif grep -qiE 'Connection refused|Connection timed out|No route to host|Network is unreachable' <<<"${out}"; then
    echo "tcp"
  elif grep -qiE 'kex_exchange_identification|Connection closed by|Host key verification failed|REMOTE HOST IDENTIFICATION|Permission denied|Connection reset' <<<"${out}"; then
    echo "ssh-handshake"
  elif grep -qiE 'session request failed|mux_client_request_session|channel.*open failed|Exec request failed' <<<"${out}"; then
    echo "request"
  elif [[ "${rc}" -ne 0 ]]; then
    echo "command-exit"
  else
    echo "ok"
  fi
}

declare -A counts
declare -A samples
for layer in dns tcp ssh-handshake request post-command-255 command-exit ok; do counts[$layer]=0; done

echo "== ssh-255 probe cell=${CELL} pod=${pod_ip} count=${COUNT} =="
ok_time=0.0
ok_n=0
for ((i = 1; i <= COUNT; i++)); do
  start="$(date +%s%N)"
  out="$(ssh -vv -p "${tool_port}" -i "${key_dir}/id_ed25519" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${key_dir}/known_hosts" \
    "executor@${pod_ip}" 'echo probe-ok' 2>&1)" || rc=$?
  rc="${rc:-0}"
  end="$(date +%s%N)"
  layer="$(classify "${rc}" "${out}")"
  counts[$layer]=$((counts[$layer] + 1))
  if [[ "${layer}" == ok ]]; then
    ok_time=$(awk -v a="${ok_time}" -v b=$(( (end - start) / 1000000 )) 'BEGIN{printf "%.1f", a + b}')
    ok_n=$((ok_n + 1))
  else
    if [[ -z "${samples[$layer]:-}" ]]; then
      samples[$layer]="$(printf '%s' "${out}" | grep -iE 'connect to address|debug1:.*(connect|server host key|Authentications)|Connection (closed|reset|refused)|kex|session|Permission denied' | head -3 | tr '\n' ';')"
    fi
  fi
  if (( i % 100 == 0 )); then
    echo "  ${i}/${COUNT} done: ok=${counts[ok]} dns=${counts[dns]} tcp=${counts[tcp]} handshake=${counts[ssh-handshake]} request=${counts[request]} post255=${counts[post-command-255]} cmd=${counts[command-exit]}"
  fi
  unset rc
done

echo "== results =="
printf '%-16s %8s\n' "layer" "count"
for layer in ok dns tcp ssh-handshake request post-command-255 command-exit; do
  printf '%-16s %8d\n' "${layer}" "${counts[$layer]}"
done
if (( ok_n > 0 )); then
  printf 'avg ok latency: %.1f ms\n' "$(awk -v t="${ok_time}" -v n="${ok_n}" 'BEGIN{printf "%.1f", t / n}')"
fi
echo "== failure samples (first per layer) =="
for layer in dns tcp ssh-handshake request post-command-255 command-exit; do
  if [[ -n "${samples[$layer]:-}" ]]; then
    printf '%-16s %s\n' "${layer}" "${samples[$layer]}"
  fi
done
