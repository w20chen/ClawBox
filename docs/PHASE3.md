# ClawBox Phase 1-3

## Architecture

```text
OpenClaw plugin -> Tenant Scheduler -> Global Allocator
                       |                    |
                       v                    v
                 tenant ClawTune KB    transactional lease
                       |
                       v
                Docker Controller -> exact Tool Agent
                                         |
                                   command + telemetry
                                         |
                                         +----> Scheduler KB update
```

The Scheduler is the only component that sees commands and KB data. The Allocator receives the
strict `ResourceRequest` model only. The Controller sees a ToolSpec and signed grant but no KB or
LLM data. The Tool Agent cannot choose its CPU allocation.

## Execution flow

`POST /v1/executions/run` predicts with the tenant's existing ClawTune
`RuntimeToolResourceKB`, maps P90 CPU to 4/16/32 vCPU, obtains a lease, acquires the sticky
`workspace_id -> tool_pod_uid` binding, signs an HMAC grant, executes on that exact agent, stores
one idempotent observation, advances the tenant KB generation, then releases tool and lease.

## Security boundary

- Service APIs require a bearer service identity. Replace it with mTLS/SPIFFE without changing
  domain models.
- Grants bind tenant, execution, workspace, tool UID, command digest, lease epoch/token, expiry,
  and nonce. The Tool Agent rejects expiry, tampering, wrong target, and replay.
- Observation ownership is checked against the persisted execution. Only complete, valid,
  successful observations update the trusted KB.
- PostgreSQL uniqueness enforces one lease per execution and one observation type/version.
- Allocator admission uses a PostgreSQL transaction-wide advisory lock plus row locks. Expired
  leases remain capacity-consuming as `LEASE_EXPIRED` until workload stop is confirmed.

Development defaults are intentionally insecure; set long random `CLAWBOX_SERVICE_TOKEN` and
`CLAWBOX_GRANT_SECRET` outside source control.

## Local deployment

Install and test without Docker:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python scripts/e2e.py
```

The e2e script starts three real HTTP services and uses the subprocess Controller backend because
it is also usable on hosts without Docker. It is a test/degraded backend, not a tenant sandbox.

Docker deployment (Linux, Docker socket available):

```bash
bash scripts/linux-deploy.sh all
```

Use `bash scripts/linux-deploy.sh init` to generate configuration only. Existing secrets are
preserved, including the generated PostgreSQL password. `deploy`, `verify`, `status`, `logs`, and
`down` provide the remaining lifecycle operations; `down` deliberately preserves the PostgreSQL
volume.

All control-plane services have console entry points: `clawbox-scheduler`, `clawbox-allocator`,
`clawbox-controller`, `clawbox-node-agent`, and `clawbox-tool-agent`.

## Kubernetes and Kata/Firecracker deployment

The existing `deploy/cell.yaml`, RBAC-free tenant cell, NetworkPolicy, RuntimeClass, and
`deploy/runtimeclass.yaml` remain the Phase 4/6 base. Phase 3 deliberately does not claim a
Kubernetes Controller backend. Existing cells can still run with `kata-fc`; the new control plane
uses Docker until Phase 4 replaces the backend. Kubernetes manifests for these new services,
Guaranteed QoS, static CPU Manager and topology enforcement are therefore tracked below as MVP
work, not falsely marked complete.

## Failure semantics

- Tool non-zero exit and invalid/incomplete telemetry are persisted for diagnosis but do not
  enter the trusted KB.
- Duplicate observations return the existing generation and do not train twice.
- Scheduler restart restores executions, KB snapshots, bindings, and leases from PostgreSQL.
- Allocator restart changes its epoch. Existing leases retain their original epoch/token.
- A lease timeout is not physical stop. It becomes `LEASE_EXPIRED`, remains charged, and can only
  become reclaimable/released after workload-stop confirmation.
- Controller create failure unwinds the lease when safe; a missing stop confirmation retains
  capacity conservatively.

## Remaining work

Required for MVP beyond Phase 3:

- Phase 4 Kubernetes Controller backend, dedicated ServiceAccounts/RBAC/NetworkPolicies for the
  new services, Guaranteed QoS validation, and sticky Pod endpoint discovery.
- Integrate ClawTune `claw-launch` inside the new Tool Agent container with a writable delegated
  per-execution cgroup and feed existing eBPF telemetry into `Observation`.
- Persist command-independent execution recovery metadata so an operator/reconciler can finish
  orphan cleanup automatically, plus periodic allocator/controller reconciliation.
- Replace the Phase 3 process collector's placeholder peak memory with cgroup-v2 measurements.

Future research optimization:

- Better P90-to-size calibration, public compatibility classifiers, NUMA load-aware placement,
  PMU/LLC/memory-bandwidth signals, and advanced fairness.

Production hardening:

- mTLS/workload identity, encrypted secrets, audited image allowlists/digests, PostgreSQL
  migrations, structured audit logs/metrics, HA leader election, rate limiting, and key rotation.
