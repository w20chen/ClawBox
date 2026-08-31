"""Scalable isolated IPv4 allocation for direct-Firecracker sessions."""

from __future__ import annotations

import argparse
import ipaddress
from dataclasses import dataclass


SESSION_PREFIX_LENGTH = 29


@dataclass(frozen=True, slots=True)
class SessionNetwork:
    network: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address
    runtime: ipaddress.IPv4Address
    tool: ipaddress.IPv4Address


def parse_network(value: str) -> ipaddress.IPv4Network:
    """Parse an IPv4 parent CIDR large enough to contain /29 sessions."""
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid network CIDR {value!r}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("direct-Firecracker networking requires IPv4")
    if network.prefixlen > SESSION_PREFIX_LENGTH:
        raise ValueError(
            f"network {network} is smaller than one /{SESSION_PREFIX_LENGTH} session"
        )
    return network


def session_capacity(network: ipaddress.IPv4Network) -> int:
    return network.num_addresses // (1 << (32 - SESSION_PREFIX_LENGTH))


def session_network(network: ipaddress.IPv4Network, index: int) -> SessionNetwork:
    if index < 0 or index >= session_capacity(network):
        raise ValueError(
            f"session index {index} is outside {network}'s "
            f"{session_capacity(network)}-session capacity"
        )
    block_size = 1 << (32 - SESSION_PREFIX_LENGTH)
    subnet = ipaddress.ip_network(
        (int(network.network_address) + index * block_size, SESSION_PREFIX_LENGTH)
    )
    return SessionNetwork(
        network=subnet,
        gateway=subnet.network_address + 1,
        runtime=subnet.network_address + 2,
        tool=subnet.network_address + 3,
    )


def guest_mac(index: int, host: int) -> str:
    """Return a stable locally administered MAC without an 8-bit session cap."""
    if index < 0 or index >= 1 << 24:
        raise ValueError("session index does not fit the MAC allocation")
    if host not in {2, 3}:
        raise ValueError("guest host selector must be Runtime (2) or Tool (3)")
    return (
        f"06:30:{(index >> 16) & 0xff:02x}:{(index >> 8) & 0xff:02x}:"
        f"{index & 0xff:02x}:{host:02x}"
    )


def static_ip_argument(address: ipaddress.IPv4Address, gateway: ipaddress.IPv4Address) -> str:
    netmask = ipaddress.IPv4Network(f"0.0.0.0/{SESSION_PREFIX_LENGTH}").netmask
    return f"ip={address}::{gateway}:{netmask}::eth0:off"


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capacity_parser = subparsers.add_parser("capacity")
    capacity_parser.add_argument("network")
    session_parser = subparsers.add_parser("session")
    session_parser.add_argument("network")
    session_parser.add_argument("index", type=int)
    args = parser.parse_args()
    network = parse_network(args.network)
    if args.command == "capacity":
        print(session_capacity(network))
        return
    allocated = session_network(network, args.index)
    print(f"{allocated.gateway}/{SESSION_PREFIX_LENGTH}")
    print(allocated.gateway)
    print(allocated.runtime)
    print(allocated.tool)


if __name__ == "__main__":
    main()
