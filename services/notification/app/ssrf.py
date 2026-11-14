"""SSRF guard for webhook URLs.

A subscription URL is attacker-controlled input that this service will later
request from inside the network. Rules:

- ``https`` only (``http`` only for hosts on the development allow-list)
- no credentials in the URL, no fragments
- every address the host resolves to must be public (not private, loopback,
  link-local, multicast, reserved or unspecified)
- checked when the subscription is created **and again before every delivery**,
  because DNS can change in between
- redirects are never followed (the dispatcher sets ``follow_redirects=False``)
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrl(ValueError):
    pass


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=3.0)
    except (OSError, TimeoutError):
        raise UnsafeUrl("The host could not be resolved.") from None
    return sorted({info[4][0] for info in infos})


async def check_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    allow_http: bool = False,
    resolver: Resolver = system_resolver,
) -> str:
    """Return the normalised URL, or raise ``UnsafeUrl`` with a user-facing reason."""
    if not isinstance(url, str) or len(url) > 2000 or any(c in url for c in "\r\n\t "):
        raise UnsafeUrl("The URL is malformed.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeUrl("The URL is malformed.") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        raise UnsafeUrl("The URL must start with https://.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("The URL must not contain credentials.")
    port = port or (443 if scheme == "https" else 80)
    on_allow_list = f"{host}:{port}" in allowed_hosts
    if scheme == "http" and not (allow_http and on_allow_list):
        raise UnsafeUrl("The URL must start with https://.")
    if not on_allow_list:
        try:  # a literal IP is checked directly, never resolved
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = await resolver(host, port)
        if not addresses or not all(is_public_ip(a) for a in addresses):
            raise UnsafeUrl("The URL must point to a public address.")
    return parts._replace(fragment="").geturl()
