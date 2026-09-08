"""Evaluation contracts may use controlled transports, never external sockets."""
from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def no_evaluation_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Evaluation contract attempted external network I/O")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
