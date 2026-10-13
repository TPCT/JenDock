"""The network boundary of a connection: only addresses inside the environment's network.

The worker passes the subnets of the environment's compose network (and the gateway addresses,
which are the host itself) to the helper container. The helper resolves the configured host
inside that network and refuses to connect unless every resolved address is inside a subnet and
none is a gateway, so a connection cannot be used to reach the host, other networks or the
internet, whatever name or address is configured.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from shiftcodes.inspector.model import ErrorCode, InspectorError

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Allowed subnets (CIDR strings) and forbidden addresses (the network gateways)."""

    subnets: tuple[str, ...]
    blocked: tuple[str, ...] = ()


def _denied(message: str) -> InspectorError:
    return InspectorError(ErrorCode.NETWORK_DENIED, message)


def _parse(policy: NetworkPolicy) -> tuple[list[IpNetwork], set[str]]:
    try:
        subnets = [ipaddress.ip_network(value, strict=False) for value in policy.subnets]
        blocked = {str(ipaddress.ip_address(item)) for item in policy.blocked}
    except ValueError as error:
        raise InspectorError(ErrorCode.BAD_REQUEST, "invalid network policy") from error
    return subnets, blocked


def resolve_address(host: str, port: int, policy: NetworkPolicy) -> str:
    """Resolve ``host`` and return one address inside the policy; connect to that address."""
    if not policy.subnets:
        raise _denied("the environment network has no known subnet")
    subnets, blocked = _parse(policy)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise InspectorError(
            ErrorCode.CONNECT_FAILED, f"host {host!r} does not resolve in the environment network"
        ) from error
    addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    for address in addresses:
        inside = any(address in subnet for subnet in subnets)
        if not inside or str(address) in blocked:
            raise _denied(
                f"host {host!r} resolves outside the environment network; "
                "only services on the environment's own compose network may be inspected"
            )
    return str(addresses[0])
