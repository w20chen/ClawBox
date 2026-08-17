#!/bin/bash
# Find all openclaw dist files containing the remoteWorkspaceDir computation
# and print the exact minified text to patch.
cd /tmp/oclaw-pkg || exit 1
echo "=== files with pattern ==="
grep -rl 'remoteWorkspaceDir: path.posix.join' package/dist | head -20
echo "=== count per file ==="
for f in $(grep -rl 'remoteWorkspaceDir: path.posix.join' package/dist); do
  echo "$f: $(grep -o 'remoteWorkspaceDir: path.posix.join' "$f" | wc -l)"
done
echo "=== exact snippet ==="
grep -oh 'remoteWorkspaceDir: path.posix.join(runtimeRootDir, "workspace")' package/dist | sort -u
echo "=== context around each match (resolveSshRuntimePaths) ==="
python3 - <<'PY'
import re, glob
for f in glob.glob('package/dist/*.js'):
    s = open(f, encoding='utf-8', errors='replace').read()
    for m in re.finditer(r'remoteWorkspaceDir:\s*path\.posix\.join\(runtimeRootDir,\s*"workspace"\)', s):
        print(f, '=>', s[max(0,m.start()-220):m.end()+120].replace('\n',' '))
        print('---')
PY
