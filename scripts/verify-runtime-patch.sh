#!/bin/bash
# Verify the openclaw sandbox-root patch inside the rebuilt runtime image.
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
docker run --rm --entrypoint /bin/sh 127.0.0.1:5000/clawbox/runtime-arm64:dev -c '
D="$(npm root -g)/openclaw/dist"
echo "dist=$D"
echo "=== files containing replacement ==="
grep -l "remoteWorkspaceDir: workspaceRoot" "$D"/*.js || echo "NONE FOUND (bad)"
echo "=== resolveSshRuntimePaths snippet ==="
grep -l "remoteWorkspaceDir: workspaceRoot" "$D"/*.js | head -1 | xargs -I{} sh -c "grep -o 'function resolveSshRuntimePaths.\{0,320\}' {} | head -1"
echo "=== entrypoint pre-create present ==="
grep -c "pre-creating sandbox runtime root" /usr/local/bin/runtime-entrypoint
'
echo "=== done ==="
