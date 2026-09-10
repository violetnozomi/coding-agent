"""Evaluation contracts may use controlled transports, never external sockets."""
from __future__ import annotations

import socket

import pytest


@pytest.fixture
def synthetic_agent_revision(monkeypatch):
    """Freeze synthetic contracts to this checkout, not a historical live run."""
    from evaluation.linux_baseline import catalog, live, runner

    revision = runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()
    for module in (catalog, live, runner):
        monkeypatch.setattr(module, "AGENT_REVISION", revision)
    return revision


@pytest.fixture(autouse=True)
def no_evaluation_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Evaluation contract attempted external network I/O")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
