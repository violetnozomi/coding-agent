"""Tests for batched file writes."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_write_files_batch_creates_files_without_overwrite(tmp_path):
    from nz_coder import config
    from nz_coder.tools.files import write_files_batch

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        result = write_files_batch([
            {"path": "demo/a.txt", "content": "alpha", "purpose": "test"},
            {"path": "demo/b.txt", "content": "beta", "purpose": "test"},
        ])
        assert "Batch write completed" in result
        assert (tmp_path / "demo" / "a.txt").read_text(encoding="utf-8") == "alpha"
        assert (tmp_path / "demo" / "b.txt").read_text(encoding="utf-8") == "beta"
    finally:
        config.WORKDIR = old


def test_write_files_batch_rolls_back_on_write_failure(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.tools import files as files_mod

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    original = Path.write_text

    def flaky(self, content, *args, **kwargs):
        if self.name == "b.txt":
            raise OSError("boom")
        return original(self, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    try:
        with pytest.raises(OSError):
            files_mod._write_files_batch_impl([
                {"path": "demo/a.txt", "content": "alpha", "purpose": "test"},
                {"path": "demo/b.txt", "content": "beta", "purpose": "test"},
            ])
        assert not (tmp_path / "demo" / "a.txt").exists()
        assert not (tmp_path / "demo" / "b.txt").exists()
    finally:
        config.WORKDIR = old
