# openEuler / Kunpeng ARM64 production runbook

This runbook is the only production host path. It assumes a dedicated ARM64 machine, `/dev/kvm`, cgroup v2, two unused whole disks, and administrative access. Firecracker cannot run a guest of a different architecture, so host, Kata artifacts, task images, and Tool Bridge must all be ARM64.

## Gates

| Gate | Command | Pass condition |
|---|---|---|
| FC-0 artifact audit | `scripts/audit-kata-firecracker-arm64.sh` | Firecracker 1.12.1, Kata 3.31+, ARM64 ELF shim/kernel, block rootfs, explicit FC config |
| FC-1 assembly | `scripts/build-kata-firecracker-arm64.sh` | publisher checksums verified; native ARM64 artifact passes FC-0 |
| FC-2 storage | `scripts/setup-devmapper-openeuler-arm64.sh` | real LVM thin pool and healthy containerd devmapper plugin |
| FC-3 handler | `deploy/check-host.sh` | containerd handler, ConfigPath and snapshotter agree |
| FC-4 RuntimeClass | `deploy/check-host.sh` | handler, overhead and ready-node selector agree |
| FC-5 live proof | `scripts/arm64-kata-smoke.sh` | two isolated ARM64 Pods, different boot IDs, Service/NetworkPolicy checks, FC host processes, clean snapshots |

No RuntimeClass should be applied before FC-0 through FC-3 pass.

## Read-only inventory and plan

```bash
python3 scripts/collect-node-capacity.py > node-inventory.json

bash scripts/bootstrap-openeuler-arm64.sh plan \
  --advertise-address 10.0.0.10 \
  --devmapper-data-device /dev/nvme1n1 \
  --devmapper-meta-device /dev/nvme2n1

bash scripts/setup-devmapper-openeuler-arm64.sh plan \
  --data-device /dev/nvme1n1 \
  --metadata-device /dev/nvme2n1
```

Review `lsblk -f`, `findmnt`, multipath/LVM ownership and the canonical device names printed by the plan. The storage script rejects partitions, mounted devices, loop devices, devices with children/holders, the root backing chain, and the repository backing chain.

## Apply

The following command is destructive only to the two exact disks named in `--confirm-erase`. Replaced configuration and `/opt/kata` are copied to `/var/lib/clawbox-bootstrap/backups`.

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --advertise-address 10.0.0.10 \
  --devmapper-data-device /dev/nvme1n1 \
  --devmapper-meta-device /dev/nvme2n1 \
  --confirm-erase /dev/nvme1n1,/dev/nvme2n1
```

The bootstrap installs pinned dependencies, configures kubelet reservations and `maxPods`, initializes Kubernetes and Calico, assembles Kata/Firecracker, creates the thin pool, installs the containerd handler, then runs the host and live smoke gates. It does not automatically reset or adopt an unrelated cluster.

For an existing reviewed Kubernetes installation, execute the stages individually:

```bash
bash scripts/build-kata-firecracker-arm64.sh build --output /srv/clawbox/kata-fc
sudo bash scripts/build-kata-firecracker-arm64.sh install

sudo bash scripts/setup-devmapper-openeuler-arm64.sh apply \
  --data-device /dev/nvme1n1 --metadata-device /dev/nvme2n1 \
  --confirm-erase /dev/nvme1n1,/dev/nvme2n1

sudo systemctl restart containerd kubelet
bash deploy/check-host.sh --runtime-class kata-fc-arm64
kubectl apply -f deploy/runtimeclass-firecracker.yaml
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

## Acceptance evidence

Archive all of the following with the deployment revision:

- `/var/lib/clawbox-bootstrap/firecracker-audit.env` and `versions.env`;
- `containerd config dump` showing `kata-fc-arm64`, `devmapper`, and the audited ConfigPath;
- `file` and `--version` output for Firecracker and the Kata shim;
- RuntimeClass YAML including `podFixed` overhead;
- smoke output showing two distinct guest boot IDs and at least two Firecracker host processes;
- before/after active devmapper snapshot counts and thin-pool data/metadata percentages;
- node inventory JSON and the applied capacity ConfigMap.

The smoke fails if it observes shared filesystem configuration, hotplug assumptions, shared host storage, an unexpected VMM process, a reachable forbidden network path, or leaked active snapshots.

## Recovery

Do not use an automated cluster reset. Stop kubelet/containerd, inspect the exact backup timestamp, and restore only reviewed files from `/var/lib/clawbox-bootstrap/backups`. LVM removal is intentionally not automated because it destroys task image data. Remove the ready label immediately while investigating:

```bash
kubectl label node NODE clawbox.openai.com/firecracker-ready-
sudo bash scripts/setup-devmapper-openeuler-arm64.sh status
sudo bash scripts/bootstrap-openeuler-arm64.sh status
```
