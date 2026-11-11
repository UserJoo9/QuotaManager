"""Dual-stack web listener configuration and fallback behavior."""

from __future__ import annotations

import errno
import socket

from core.config import WebConfig
from quota import web_listener


def test_default_web_host_is_dual_stack_but_numeric_ipv4_wildcard_is_not():
    assert WebConfig().host == "*"

    targets, optional = web_listener._listener_targets("*", 8080)
    assert {target[0] for target in targets} == {socket.AF_INET, socket.AF_INET6}
    assert optional is True

    targets, optional = web_listener._listener_targets("0.0.0.0", 8080)
    assert [target[0] for target in targets] == [socket.AF_INET]
    assert optional is False


def test_explicit_ipv6_wildcard_stays_ipv6_only():
    targets, optional = web_listener._listener_targets("::", 8080)
    assert [target[0] for target in targets] == [socket.AF_INET6]
    assert optional is False


def test_optional_ipv6_socket_constructor_failure_keeps_ipv4_listener(monkeypatch):
    real_socket = socket.socket

    def socket_without_ipv6(family, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "IPv6 is unavailable")
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(web_listener.socket, "socket", socket_without_ipv6)
    listeners = web_listener.make_web_listener_sockets("*", 0)
    try:
        assert len(listeners) == 1
        assert listeners[0].family == socket.AF_INET
    finally:
        for listener in listeners:
            listener.close()
