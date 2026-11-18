"""SSRF guard for outbound connections to customer databases (SECURITY_ARCHITECTURE §8, DE design §2).

    pinned = resolve_and_check("db.example.com", 5432)      # -> ResolvedHost(host, ip, port)
    psycopg.connect(host=pinned.host, hostaddr=pinned.ip, ...)   # libpq never resolves the name again

Rules:
  * the host name is syntax-checked (no URLs, paths, spaces, user-info, ports) and names such as `localhost`
    or `*.internal` are refused before any DNS lookup;
  * DNS resolution happens ONCE; EVERY returned address must be allowed (a mixed public/private answer is
    refused), and the first allowed address is pinned. The connection then uses `hostaddr=<pinned ip>` so a
    second (rebinding) lookup can never redirect it;
  * loopback, RFC 1918 / ULA private, link-local, CGNAT (100.64/10), reserved, documentation/benchmark ranges
    and IPv4-mapped / 6to4 / Teredo embeddings of those are refused unless `settings.ALLOW_PRIVATE_DB_HOSTS`
    (dev only). Unspecified (0.0.0.0, ::), multicast/broadcast and cloud metadata endpoints are ALWAYS refused;
  * optional extra deny CIDRs (platform subnets) come from `settings.CONNECTOR_DENY_CIDRS` (list, optional).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

from django.conf import settings

from .exceptions import HostForbidden, HostUnresolvable, InvalidHost

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_FORBIDDEN_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
                    "metadata.google.internal", "instance-data")
_FORBIDDEN_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa")

# Always refused, even with ALLOW_PRIVATE_DB_HOSTS (cloud metadata services).
_METADATA_NETS = tuple(ipaddress.ip_network(n) for n in (
    "169.254.169.254/32", "169.254.170.2/32", "fd00:ec2::254/128", "100.100.100.200/32",
))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class ResolvedHost:
    host: str          # the name as configured (used for TLS SNI / certificate verification)
    ip: str            # the single pinned address libpq must connect to
    port: int


def _deny_nets() -> tuple:
    extra = getattr(settings, "CONNECTOR_DENY_CIDRS", None) or ()
    return tuple(ipaddress.ip_network(str(n), strict=False) for n in extra)


def normalize_host(host: str) -> str:
    """Validate the host syntax (hostname or IP literal) and return it lower-cased."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in host or ""):
        raise InvalidHost("The host contains control characters.")
    value = (host or "").strip()
    if not value or len(value) > 253:
        raise InvalidHost("The host must be a hostname or an IP address.")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if any(ch in value for ch in "/\\@:?#%= \t\r\n'\"") or "%" in value:
        raise InvalidHost("The host must be a bare hostname or IP address (no scheme, port, path or user).")
    name = value.rstrip(".").lower()
    labels = name.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise InvalidHost("The host name is not a valid DNS name.")
    if labels[-1].isdigit():
        raise InvalidHost("The host name is not a valid DNS name.")  # e.g. "127.1" shorthand forms
    return name


def _embedded_ipv4(ip: IPAddress) -> list[ipaddress.IPv4Address]:
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    out = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)
    # IPv4-compatible (deprecated) ::a.b.c.d
    if int(ip) >> 32 == 0 and int(ip) > 1:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    # NAT64 well-known prefix 64:ff9b::/96
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def classify_ip(ip: IPAddress) -> str | None:
    """None when the address is a normal public unicast address, else the reason it is special."""
    if any(ip in net for net in _METADATA_NETS if net.version == ip.version):
        return "metadata"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast or (isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("255.255.255.255")):
        return "multicast"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if isinstance(ip, ipaddress.IPv4Address) and (ip in _CGNAT):
        return "cgnat"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _BENCHMARK:
        return "reserved"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return "private"
    if not ip.is_global:
        return "reserved"
    for inner in _embedded_ipv4(ip):
        reason = classify_ip(inner)
        if reason:
            return reason
    return None


ALWAYS_FORBIDDEN = frozenset({"metadata", "unspecified", "multicast"})


def check_ip(ip: IPAddress | str, *, allow_private: bool | None = None) -> IPAddress:
    """Raise HostForbidden unless `ip` may be connected to."""
    addr = ip if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(str(ip))
    if allow_private is None:
        allow_private = bool(getattr(settings, "ALLOW_PRIVATE_DB_HOSTS", False))
    for net in _deny_nets():
        if net.version == addr.version and addr in net:
            raise HostForbidden("The host resolves to a platform network that connectors may not reach.",
                                extra={"reason": "platform_network"})
    reason = classify_ip(addr)
    if reason is None:
        return addr
    if reason in ALWAYS_FORBIDDEN or not allow_private:
        raise HostForbidden(
            f"The host resolves to a {reason.replace('_', '-')} address, which connectors may not reach.",
            extra={"reason": reason},
        )
    return addr


def check_name(name: str, *, allow_private: bool | None = None) -> None:
    """Refuse well-known internal names before any lookup (unless private hosts are allowed)."""
    if allow_private is None:
        allow_private = bool(getattr(settings, "ALLOW_PRIVATE_DB_HOSTS", False))
    if name in ("metadata", "metadata.google.internal", "instance-data"):
        raise HostForbidden("Cloud metadata hosts are never allowed.", extra={"reason": "metadata"})
    if allow_private:
        return
    if name in _FORBIDDEN_NAMES or name.endswith(_FORBIDDEN_SUFFIXES):
        raise HostForbidden("Internal host names are not allowed.", extra={"reason": "internal_name"})


def precheck_host(host: str) -> str:
    """Synchronous, network-free check used when a data source is saved (the full check runs on the worker)."""
    name = normalize_host(host)
    try:
        check_ip(ipaddress.ip_address(name))
    except ValueError:
        check_name(name)
    return name


def resolve_and_check(host: str, port: int, *, allow_private: bool | None = None,
                      resolver=socket.getaddrinfo) -> ResolvedHost:
    """Resolve ONCE, require every address to be allowed and pin the first one."""
    if not (1 <= int(port) <= 65535):
        raise InvalidHost("The port must be between 1 and 65535.")
    name = normalize_host(host)
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        check_ip(literal, allow_private=allow_private)
        return ResolvedHost(host=name, ip=str(literal), port=int(port))
    check_name(name, allow_private=allow_private)
    try:
        infos = resolver(name, int(port), 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise HostUnresolvable("The host name could not be resolved.") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        raw = info[4][0]
        if isinstance(raw, str) and "%" in raw:  # scoped IPv6 (fe80::1%eth0) -> link-local, refuse below
            raw = raw.split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise HostUnresolvable("The host name did not resolve to any address.")
    for addr in addresses:  # every answer must pass: a mixed public/private answer is an attack signal
        check_ip(addr, allow_private=allow_private)
    pinned = next((a for a in addresses if a.version == 4), addresses[0])
    return ResolvedHost(host=name, ip=str(pinned), port=int(port))
