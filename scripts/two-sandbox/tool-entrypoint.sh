#!/usr/bin/env bash
set -euo pipefail

: "${TOOL_EXEC_TIMEOUT_SECONDS:=300}"
: "${TOOL_PIDS_LIMIT:=128}"

case "${TOOL_EXEC_TIMEOUT_SECONDS}" in (*[!0-9]*|'') echo "invalid TOOL_EXEC_TIMEOUT_SECONDS" >&2; exit 64;; esac
case "${TOOL_PIDS_LIMIT}" in (*[!0-9]*|'') echo "invalid TOOL_PIDS_LIMIT" >&2; exit 64;; esac

mkdir -p /run/tool-sshd /home/executor/.ssh /tmp/openclaw-sandboxes
chmod 0700 /home/executor/.ssh /tmp/openclaw-sandboxes
install -m 0600 /var/run/secrets/tool-ssh/id_ed25519.pub /home/executor/.ssh/authorized_keys

test -s /var/run/secrets/tool-ssh/ssh_host_ed25519_key
test -s /var/run/secrets/tool-ssh/ssh_host_ed25519_key.pub
install -m 0600 /var/run/secrets/tool-ssh/ssh_host_ed25519_key /run/tool-sshd/ssh_host_ed25519_key

cat >/run/tool-sshd/sshd_config <<'EOF'
Port 2222
ListenAddress 0.0.0.0
HostKey /run/tool-sshd/ssh_host_ed25519_key
PidFile /run/tool-sshd/sshd.pid
AuthorizedKeysFile /home/executor/.ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
PermitEmptyPasswords no
PermitRootLogin no
AllowUsers executor
UsePAM no
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
GatewayPorts no
PermitUserEnvironment no
PermitUserRC no
StrictModes yes
LogLevel VERBOSE
ForceCommand /usr/local/bin/tool-command
EOF

echo "[tool-sandbox] tenant_id=${TENANT_ID:-unknown} runtime_id=${RUNTIME_ID:-unknown} sandbox=tool ssh_port=2222" >&2
exec /usr/sbin/sshd -D -e -f /run/tool-sshd/sshd_config
