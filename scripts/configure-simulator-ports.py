#!/usr/bin/env python3
"""Preview/apply persistent Linux reservations for managed simulator ports."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def port_set(value):
    ports = set()
    for part in value.strip().split(','):
        if not part:
            continue
        bounds = part.split('-')
        first, last = int(bounds[0]), int(bounds[-1])
        if len(bounds) > 2 or not 1 <= first <= last <= 65535:
            raise ValueError(f'invalid reserved-port range: {part}')
        ports.update(range(first, last + 1))
    return ports


def ranges(ports):
    groups = []
    for port in sorted(ports):
        if groups and port == groups[-1][1] + 1:
            groups[-1][1] = port
        else:
            groups.append([port, port])
    return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-port', required=True, type=int)
    parser.add_argument('--cluster-count', type=int, default=1)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    last = args.base_port + args.cluster_count - 1
    if args.cluster_count < 1 or not 1 <= args.base_port <= last <= 65535:
        parser.error('base port and cluster count must describe ports within 1..65535')
    if sys.platform != 'linux':
        parser.error('run this command on the Linux experiment host')
    kernel = Path('/proc/sys/net/ipv4/ip_local_reserved_ports')
    config = Path('/etc/sysctl.d/99-clawbox-simulator-ports.conf')
    previous = kernel.read_text().strip()
    ports = port_set(previous)
    # Preserve reservations previously persisted by this script even if someone
    # temporarily changed the runtime sysctl after boot.
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith('net.ipv4.ip_local_reserved_ports='):
                ports.update(port_set(line.split('=', 1)[1]))
    ports.update(range(args.base_port, last + 1))
    proposed = ranges(ports)
    if args.apply:
        if os.geteuid() != 0:
            parser.error('apply requires sudo; preview without --apply first')
        subprocess.run(['sysctl', '-w', f'net.ipv4.ip_local_reserved_ports={proposed}'],
                       check=True, stdout=subprocess.PIPE, text=True)
        if port_set(kernel.read_text()) != ports:
            raise RuntimeError('reserved-port readback differs; inspect sysctl before launching')
        config.write_text('# Managed by scripts/configure-simulator-ports.py\n'
                          f'net.ipv4.ip_local_reserved_ports={proposed}\n')
    print(json.dumps(dict(applied=args.apply, previous=previous, proposed=proposed,
                          config=str(config), ports=[args.base_port, last])))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'configure-simulator-ports: {exc}', file=sys.stderr)
        sys.exit(1)
