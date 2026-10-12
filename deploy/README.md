# CubeSandbox deployment components

Use the [installation guide](../docs/installation.md) for the complete host setup.
`cubesandbox/prepare-semantic-source.sh` applies the pinned server patches for
VM TCP endpoint discovery, routing to the registered node, image identity,
snapshot storage, memory isolation, AgentCgroup priority placement, and the
configurable Go build proxy. [build-cubesandbox-release.sh](../scripts/build-cubesandbox-release.sh)
prepares these sources and builds the release with the exported guest kernel.
`cubesandbox/tiered-memory.conf`
contains the service settings for disk-backed snapshots. Use the matching
server and Python SDK.
