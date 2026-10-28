# CubeSandbox deployment components

Use the [installation guide](../docs/installation.md) for the complete host setup.
`cubesandbox/prepare-semantic-source.sh` prepares CubeSandbox v0.7.0 with patches for
per-VM TCP endpoint discovery, routing to the registered node, image identity,
snapshot storage, memory isolation, AgentCgroup priority placement,
bounded SDK control/lifecycle requests, guest-agent reconnection, and the
configurable Go build proxy. [build-cubesandbox-release.sh](../scripts/build-cubesandbox-release.sh)
prepares these sources and builds the release with the exported guest kernel.
`cubesandbox/tiered-memory.conf`
contains the service settings for disk-backed snapshots. Use the matching
server and Python SDK. The builder does not install binaries or restart services.
Use the [update table](../docs/installation.md#update-an-existing-host) to determine
whether a change needs an SDK reinstall, server deployment, or guest image rebuild.
