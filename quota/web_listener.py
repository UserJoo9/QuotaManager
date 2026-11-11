"""Explicit IPv4/IPv6 sockets for the existing Uvicorn web listener."""

from __future__ import annotations

import errno
import ipaddress
import socket


def _listener_targets(host: str, port: int) -> tuple[list[tuple[int, int, int, tuple]], bool]:
    """Resolve configured host to bind targets and whether IPv6 is optional."""
    if host in ("", "*"):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
             ("0.0.0.0", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
             ("::", port, 0, 0)),
        ], True

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        records = socket.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        targets = []
        seen = set()
        for family, socktype, proto, _, sockaddr in records:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            key = (family, sockaddr[0])
            if key in seen:
                continue
            seen.add(key)
            targets.append((family, socktype, proto, sockaddr))
        if not targets:
            raise OSError(f"web host {host!r} has no IPv4 or IPv6 address")
        return targets, len({target[0] for target in targets}) > 1

    if address.is_unspecified and address.version == 4:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 ("0.0.0.0", port))], False
    if address.is_unspecified:
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 ("::", port, 0, 0))], False
    if address.version == 4:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 (str(address), port))], False
    return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
             (str(address), port, 0, 0))], False


def make_web_listener_sockets(host: str, port: int) -> list[socket.socket]:
    """Bind configured addresses on one port without IPv4-mapped overlap.

    Wildcard and dual-address hostnames get one explicit socket per family.
    A specific address keeps its original reachability scope. If the machine
    has no IPv6 support, an optional IPv6 wildcard/hostname target is skipped
    while the IPv4 listener remains available.
    """
    targets, ipv6_optional = _listener_targets(host, int(port))
    bound: list[socket.socket] = []
    selected_port = int(port)
    try:
        for family, socktype, proto, sockaddr in targets:
            address = list(sockaddr)
            address[1] = selected_port
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(tuple(address))
            except OSError as exc:
                if sock is not None:
                    sock.close()
                if (family == socket.AF_INET6 and ipv6_optional and
                        exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT,
                                      errno.EADDRNOTAVAIL, errno.ENOPROTOOPT,
                                      getattr(errno, "EPFNOSUPPORT", -1),
                                      getattr(errno, "EOPNOTSUPP", -1)}):
                    continue
                raise
            if selected_port == 0:
                selected_port = int(sock.getsockname()[1])
            bound.append(sock)
        if not bound:
            raise OSError("no web listener socket could be bound")
        return bound
    except Exception:
        for sock in bound:
            sock.close()
        raise
