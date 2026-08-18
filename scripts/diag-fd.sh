#!/usr/bin/env bash
# Diagnostic: FD limits + usage on the target (run with: bash diag-fd.sh)
echo "=== system file-nr ==="
cat /proc/sys/fs/file-nr

echo "=== containerd processes ==="
for p in $(pgrep -x containerd); do
  echo "pid $p limits:"; grep "open files" "/proc/$p/limits"
done

echo "=== kata shims (first 5) ==="
count=0
for p in $(pgrep -f containerd-shim-kata); do
  echo "pid $p limits:"; grep "open files" "/proc/$p/limits"
  count=$((count + 1))
  [[ "${count}" -ge 5 ]] && break
done

echo "=== open fd count per kata shim ==="
for p in $(pgrep -f containerd-shim-kata); do
  n="$(ls "/proc/$p/fd" 2>/dev/null | wc -l)"
  echo "shim $p: $n fds"
done
