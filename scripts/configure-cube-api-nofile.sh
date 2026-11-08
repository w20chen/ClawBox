#!/usr/bin/env bash
# Persist the CubeAPI file-descriptor floor and update its current process.
set -Eeuo pipefail
export LC_ALL=C

service=cube-sandbox-cube-api.service
dropin=/etc/systemd/system/cube-sandbox-cube-api.service.d/90-clawbox-nofile.conf
minimum=65536
apply=false

usage() {
  cat <<'EOF'
Usage: bash scripts/configure-cube-api-nofile.sh [--minimum N] [--apply]

Preview CubeAPI's open-file limit repair; only --apply changes the host.
  --minimum N  Minimum descriptors per process (positive integer; default 65536)
  --apply      Install the systemd drop-in and raise the live limit; requires root
  -h, --help   Show this help

Higher existing limits are preserved. This does not restart or start services.
EOF
}
die() { echo "ERROR: $*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --minimum)
      (($# >= 2)) || die '--minimum requires a positive integer'
      minimum=$2; shift 2 ;;
    --apply) apply=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1; use --help" ;;
  esac
done
[[ $minimum =~ ^[1-9][0-9]*$ ]] || die '--minimum must be a positive decimal integer'

# Compare decimal strings without overflowing shell integers.
at_least() {
  [[ $1 == infinity ]] && return 0
  [[ $2 == infinity ]] && return 1
  ((${#1} > ${#2})) || {
    ((${#1} == ${#2})) && [[ $1 == "$2" || $1 > "$2" ]]
  }
}
normalize() {
  case "$1" in
    infinity|unlimited|18446744073709551615) echo infinity ;;
    *) [[ $1 =~ ^[0-9]+$ ]] || die "invalid open-file limit: $1"; echo "$1" ;;
  esac
}
maximum() { if at_least "$1" "$2"; then echo "$1"; else echo "$2"; fi; }
property() { systemctl show "$service" --property="$1" --value; }
read_live() {
  pid=$(property MainPID) || die "cannot read $service MainPID"
  live_soft=0; live_hard=0
  if [[ $pid != 0 ]]; then
    local values
    values=$(awk '/^Max open files / {print $4, $5}' "/proc/$pid/limits") ||
      die "cannot read PID $pid limits; rerun with sudo, or rerun if CubeAPI exited"
    read -r live_soft live_hard <<<"$values"
    live_soft=$(normalize "$live_soft")
    live_hard=$(normalize "$live_hard")
  fi
}

command -v systemctl >/dev/null || die 'systemctl is required; run on the Linux CubeSandbox host'
command -v awk >/dev/null || die 'awk is required on the CubeSandbox host'
[[ $(property LoadState) == loaded ]] || die "$service is not loaded; install/configure CubeSandbox first"
[[ -r /proc/sys/fs/nr_open ]] || die 'cannot read /proc/sys/fs/nr_open; run on the Linux host'
read -r kernel_max </proc/sys/fs/nr_open
at_least "$kernel_max" "$minimum" || die "--minimum exceeds kernel fs.nr_open=$kernel_max; choose a lower value"
configured_soft=$(normalize "$(property LimitNOFILESoft)")
configured_hard=$(normalize "$(property LimitNOFILE)")
read_live
target_soft=$(maximum "$(maximum "$minimum" "$configured_soft")" "$live_soft")
target_hard=$(maximum "$(maximum "$configured_hard" "$live_hard")" "$target_soft")
content=$(printf '[Service]\nLimitNOFILE=%s:%s\n' "$target_soft" "$target_hard")

printf 'Service: %s\nConfigured soft:hard: %s:%s\n' "$service" "$configured_soft" "$configured_hard"
if [[ $pid == 0 ]]; then
  echo 'No live CubeAPI process; only its next start will use the persistent limit.'
else
  printf 'PID %s soft:hard: %s:%s\n' "$pid" "$live_soft" "$live_hard"
fi
printf 'Drop-in: %s\n%s\n' "$dropin" "$content"
if ! $apply; then
  echo 'Preview only. Add --apply with sudo to reload systemd and raise the live limit without restarting.'
  exit 0
fi
((EUID == 0)) || die 'apply requires root: sudo bash scripts/configure-cube-api-nofile.sh --apply'
command -v prlimit >/dev/null || die 'prlimit is required; install util-linux before applying'

temporary=
trap '[[ -z $temporary ]] || rm -f -- "$temporary"' EXIT
install -d -m 0755 "${dropin%/*}" || die "cannot create ${dropin%/*}"
temporary=$(mktemp "${dropin}.XXXXXX") || die 'cannot create the temporary drop-in'
printf '%s\n' "$content" >"$temporary"
chmod 0644 "$temporary"
mv -f -- "$temporary" "$dropin"
temporary=
systemctl daemon-reload || die 'drop-in written, but daemon-reload failed; inspect systemd and rerun'
effective_soft=$(normalize "$(property LimitNOFILESoft)")
effective_hard=$(normalize "$(property LimitNOFILE)")
if ! at_least "$effective_soft" "$target_soft" || ! at_least "$effective_hard" "$target_hard"; then
  die "another unit setting overrides the drop-in; inspect: systemctl cat $service"
fi

# Refresh MainPID and limits after the reload. Never lower either live limit.
read_live
if [[ $pid != 0 ]]; then
  old_hard=$live_hard
  if ! at_least "$live_soft" "$minimum"; then
    if at_least "$live_hard" "$minimum"; then
      prlimit --pid "$pid" --nofile="$minimum:" || die "persistent setting applied, but PID $pid update failed; inspect privileges/process status and rerun"
    else
      prlimit --pid "$pid" --nofile="$minimum:$minimum" || die "persistent setting applied, but PID $pid update failed; inspect privileges/kernel limits and rerun"
    fi
  fi
  applied_pid=$pid
  read_live
  [[ $pid == "$applied_pid" ]] || die 'CubeAPI PID changed during apply; rerun to verify its new process'
  at_least "$live_soft" "$minimum" && at_least "$live_hard" "$old_hard" || die 'live limits did not reach the requested floor; inspect /proc/PID/limits and rerun'
  printf 'Applied. PID %s soft:hard: %s:%s; persistent soft:hard: %s:%s.\n' "$pid" "$live_soft" "$live_hard" "$effective_soft" "$effective_hard"
else
  printf 'Applied persistent soft:hard: %s:%s; CubeAPI has no live process.\n' "$effective_soft" "$effective_hard"
fi
echo 'No service was started or restarted.'
