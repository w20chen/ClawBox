# CubeSandbox deployment components

Use the [installation guide](../docs/installation.md) for the complete host setup.
`cubesandbox/prepare-semantic-source.sh` applies the pinned server patch set:
semantic TCP endpoints, same-node routing, image provenance, snapshot storage,
and memory isolation. `cubesandbox/tiered-memory.conf` contains the service flags
used by optional memory-tier setup. The matching server and Python SDK are required.
