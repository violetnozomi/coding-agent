"""Network target validation for model-initiated outbound requests."""
from __future__ import annotations

from collections.abc import Callable, Iterable
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeNetworkTarget(ValueError):
    """A URL resolves to an address that models must not reach."""


Resolver = Callable[[str, int], Iterable[str]]
_METADATA_HOSTS = frozenset({
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "instance-data.ec2.internal",
})


class NetworkTargetPolicy:
    """Validate literals and every DNS answer before an outbound connection."""

    def __init__(
        self,
        resolver: Resolver | None = None,
        *,
        _allow_private_for_tests: bool = False,
    ) -> None:
        self._resolver = resolver or _resolve_host
        self._allow_private_for_tests = bool(_allow_private_for_tests)

    def validate_url(self, url: str) -> tuple[str, ...]:
        parsed = urlsplit(str(url))
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname:
            raise UnsafeNetworkTarget("Network target has no hostname")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            if not self._allow_private_for_tests:
                raise UnsafeNetworkTarget("Network target is local or private")
        if hostname in _METADATA_HOSTS or hostname.endswith(".metadata.google.internal"):
            raise UnsafeNetworkTarget("Cloud metadata targets are blocked")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = tuple(dict.fromkeys(str(item) for item in self._resolver(hostname, port)))
            if not addresses:
                raise UnsafeNetworkTarget("Network target did not resolve")
        else:
            addresses = (str(literal),)
        for address in addresses:
            self.validate_ip(address)
        return addresses

    def validate_ip(self, value: str) -> None:
        try:
            address = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeNetworkTarget("Network target resolved to an invalid address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if self._allow_private_for_tests:
            return
        if not address.is_global:
            raise UnsafeNetworkTarget("Network target is local, private, or non-routable")


def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeNetworkTarget("Network target DNS resolution failed") from exc
    return tuple(dict.fromkeys(str(row[4][0]) for row in rows if row[4]))


__all__ = ["NetworkTargetPolicy", "UnsafeNetworkTarget"]
