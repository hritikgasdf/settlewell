"""Block sockets in the default suite. Sandbox calls opt in explicitly."""

from __future__ import annotations

import os
import socket

import pytest

_NETWORK_BLOCKED = "unit tests must not use the network"


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    marked = request.node.get_closest_marker("sandbox") is not None
    if marked and os.environ.get("SETTLEWELL_SANDBOX") == "1":
        yield
        return

    real_socket = socket.socket

    class BlockedSocket(real_socket):
        def connect(self, address, *args, **kwargs):
            raise RuntimeError(_NETWORK_BLOCKED)

        def connect_ex(self, address, *args, **kwargs):
            raise RuntimeError(_NETWORK_BLOCKED)

    def blocked(*_args, **_kwargs):
        raise RuntimeError(_NETWORK_BLOCKED)

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", blocked)
    yield


@pytest.fixture(autouse=True)
def autonomy_off_by_default(monkeypatch: pytest.MonkeyPatch):
    """Existing tests assert today's human-approves-everything behaviour. Autonomy tests opt in."""
    monkeypatch.setenv("SETTLEWELL_AUTONOMY", "0")
    monkeypatch.setenv("SETTLEWELL_AUTO_MAX_AMOUNT", "1000")
    yield
