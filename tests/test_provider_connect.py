"""Tests for the masked provider connection persistence backend."""
from __future__ import annotations

import os

import pytest

from nz_coder.foundation import config
from nz_coder.providers.configuration import (
    clear_provider_connection_overrides,
    provider_connection,
)
from nz_coder.providers.connect import provider_connect_spec, save_provider_connection


def test_save_connection_preserves_unrelated_env_and_applies_process_state(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text("KEEP=value\nAPI_KEY=old\nAPI_KEY=duplicate\n", encoding="utf-8")
    monkeypatch.setattr(config, "API_KEY", "before")
    monkeypatch.setattr(config, "API_BASE_URL", "https://before.example/v1")
    monkeypatch.setenv("API_KEY", "before")
    monkeypatch.setenv("API_BASE_URL", "https://before.example/v1")

    try:
        spec = save_provider_connection(
            "openai-compatible",
            "secret-test-key",
            "https://api.example.test/v1/",
            workspace=tmp_path,
        )
    finally:
        connection = provider_connection("openai-compatible")
        clear_provider_connection_overrides()

    content = target.read_text(encoding="utf-8")
    assert spec.credential_name == "API_KEY"
    assert "KEEP=value" in content
    assert content.count("API_KEY=") == 1
    assert "API_KEY=secret-test-key" in content
    assert "API_BASE_URL=https://api.example.test/v1" in content
    assert connection.api_key == "secret-test-key"
    assert connection.base_url == "https://api.example.test/v1"
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_save_connection_rejects_insecure_remote_endpoint(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        save_provider_connection(
            "anthropic", "secret", "http://api.example.test", workspace=tmp_path,
        )
    assert not (tmp_path / ".env").exists()


def test_save_connection_allows_loopback_http_and_blocks_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "before")
    monkeypatch.setenv("GEMINI_API_BASE_URL", "https://before.example")
    try:
        save_provider_connection(
            "gemini", "secret", "http://127.0.0.1:9000/v1", workspace=tmp_path,
        )
    finally:
        clear_provider_connection_overrides()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    outside.write_text("SAFE=1\n", encoding="utf-8")
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").symlink_to(outside)
    try:
        with pytest.raises(ValueError, match="symbolic link"):
            save_provider_connection(
                "openai-responses", "secret", "https://api.openai.com/v1",
                workspace=tmp_path,
            )
        assert outside.read_text(encoding="utf-8") == "SAFE=1\n"
    finally:
        outside.unlink(missing_ok=True)


def test_provider_aliases_resolve_to_supported_families():
    assert provider_connect_spec("claude").provider == "anthropic"
    assert provider_connect_spec("codex").provider == "openai-responses"


def test_provider_connection_hardens_final_credential_file(tmp_path, monkeypatch):
    """Atomic replacement must harden the final .env, not only its temp file."""
    import nz_coder.providers.connect as connect

    hardened = []
    monkeypatch.setattr(
        connect,
        "harden_private_path",
        lambda path: hardened.append(os.fspath(path)),
    )
    try:
        save_provider_connection(
            "openai-compatible",
            "secret-test-key",
            "https://api.example.test/v1",
            workspace=tmp_path,
        )
    finally:
        clear_provider_connection_overrides()

    assert os.fspath(tmp_path / ".env") in hardened


def test_windows_credential_write_fails_before_replace_when_acl_cannot_apply(
    tmp_path, monkeypatch,
):
    import nz_coder.providers.connect as connect
    from nz_coder.foundation.private_paths import PrivatePathSecurity

    target = tmp_path / ".env"
    target.write_text("KEEP=old\n", encoding="utf-8")
    monkeypatch.setattr(
        connect,
        "harden_private_path",
        lambda path: PrivatePathSecurity(str(path), False, "B", "denied"),
    )

    with pytest.raises(PermissionError, match="owner-private"):
        connect._atomic_write(target, "API_KEY=secret\n", os_name="nt")

    assert target.read_text(encoding="utf-8") == "KEEP=old\n"
    assert not any(path.name.startswith("..env.") for path in tmp_path.iterdir())
