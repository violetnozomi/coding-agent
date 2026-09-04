"""Shared pytest fixtures for NZ-Coder tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_reflection_by_default(tmp_path, monkeypatch):
    from nz_coder.foundation import config

    # Product runtime state is user-owned and must never be written into a
    # repository (or the real developer profile) during tests.
    monkeypatch.setenv(
        "XDG_STATE_HOME",
        str(tmp_path.parent / f"user-state-{tmp_path.name}"),
    )
    monkeypatch.setenv(
        "XDG_CACHE_HOME",
        str(tmp_path.parent / f"user-cache-{tmp_path.name}"),
    )
    monkeypatch.setenv(
        "LOCALAPPDATA",
        str(tmp_path.parent / f"local-app-data-{tmp_path.name}"),
    )

    old_enabled = config.REFLECTION_ENABLED
    old_attempts = config.REFLECTION_MAX_ATTEMPTS
    old_lsp_write_diagnostics = config.LSP_WRITE_DIAGNOSTICS_ENABLED
    config.REFLECTION_ENABLED = False
    config.REFLECTION_MAX_ATTEMPTS = 2
    config.LSP_WRITE_DIAGNOSTICS_ENABLED = False
    try:
        yield
    finally:
        config.REFLECTION_ENABLED = old_enabled
        config.REFLECTION_MAX_ATTEMPTS = old_attempts
        config.LSP_WRITE_DIAGNOSTICS_ENABLED = old_lsp_write_diagnostics
