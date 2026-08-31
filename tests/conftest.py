"""Shared pytest fixtures for NZ-Coder tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_reflection_by_default():
    from nz_coder.foundation import config

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
