"""Isolate user storage without disabling any runtime feature globally."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_reflection_by_default(tmp_path, monkeypatch):
    # Override the old suite fixture by name: these tests do not need its
    # reflection/LSP config mutations or a ProductRunEnvironment.
    for key in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path.parent / f"user-{tmp_path.name}" / key))
