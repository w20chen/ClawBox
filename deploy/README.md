# CubeSandbox deployment components

Use the [installation guide](../docs/installation.md) for the complete host setup.
`cubesandbox/prepare-semantic-source.sh` applies the pinned server patches for
VM TCP endpoint discovery, routing to the registered node, image identity,
snapshot storage, and memory isolation. `cubesandbox/tiered-memory.conf`
contains the service settings for disk-backed snapshots. Use the matching
server and Python SDK.
