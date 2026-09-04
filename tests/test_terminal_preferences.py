"""Tests for workspace-owned terminal preferences and model interaction state."""
from __future__ import annotations

import os

import pytest

from nz_coder.interface.preferences import (
    command_keybinding,
    cycle_model_id,
    message_keybindings,
    load_terminal_preferences,
    record_recent_model,
    theme_names,
    toggle_favorite_model,
    update_terminal_preferences,
)


def test_preferences_round_trip_and_private_file(tmp_path):
    updated = update_terminal_preferences(
        workspace=tmp_path,
        theme="nord",
        tool_details="full",
        mouse=False,
    )

    assert updated.theme == "nord"
    assert load_terminal_preferences(tmp_path) == updated
    from nz_coder.foundation.user_paths import user_storage_layout

    path = user_storage_layout(tmp_path).workspace_state / "terminal" / "preferences.json"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_preferences_harden_directory_and_atomic_final_file(tmp_path, monkeypatch):
    import nz_coder.interface.preferences as preferences

    hardened = []
    monkeypatch.setattr(
        preferences,
        "harden_private_path",
        lambda path: hardened.append(os.fspath(path)),
    )

    update_terminal_preferences(workspace=tmp_path, theme="nord")

    from nz_coder.foundation.user_paths import user_storage_layout

    target = user_storage_layout(tmp_path).workspace_state / "terminal" / "preferences.json"
    assert os.fspath(target.parent) in hardened
    assert os.fspath(target) in hardened


def test_preferences_reject_unknown_theme_and_detail_level(tmp_path):
    with pytest.raises(ValueError, match="Unknown terminal theme"):
        update_terminal_preferences(workspace=tmp_path, theme="unknown")
    with pytest.raises(ValueError, match="tool_details"):
        update_terminal_preferences(workspace=tmp_path, tool_details="verbose")


def test_recent_and_favorite_models_are_deduplicated_and_cycle(tmp_path):
    record_recent_model("openai/one", workspace=tmp_path)
    record_recent_model("anthropic/two", workspace=tmp_path)
    record_recent_model("openai/one", workspace=tmp_path)

    preferences = load_terminal_preferences(tmp_path)
    assert preferences.recent_models == ("openai/one", "anthropic/two")
    assert cycle_model_id(current="openai/one", workspace=tmp_path) == "anthropic/two"
    assert toggle_favorite_model("openai/one", workspace=tmp_path) is True
    assert cycle_model_id(favorites=True, workspace=tmp_path) == "openai/one"
    assert toggle_favorite_model("openai/one", workspace=tmp_path) is False


def test_available_theme_names_are_stable():
    assert {"nzcoder", "opencode", "catppuccin", "nord", "gruvbox"} <= set(theme_names())


def test_message_keybindings_are_validated_persisted_and_resettable(tmp_path):
    updated = update_terminal_preferences(
        workspace=tmp_path,
        keybindings={"messages_next": "c-n", "messages_last_user": "none"},
    )

    assert dict(updated.keybindings) == {
        "messages_last_user": "none",
        "messages_next": "c-n",
    }
    effective = message_keybindings(load_terminal_preferences(tmp_path))
    assert effective["messages_next"] == "c-n"
    assert effective["messages_first"] == "home"
    assert command_keybinding("message-next", "Ctrl+X J", updated) == "c-n"
    assert command_keybinding("message-last-user", "Ctrl+X H", updated) == ""

    reset = update_terminal_preferences(workspace=tmp_path, keybindings={})
    assert reset.keybindings == ()
    assert message_keybindings(reset)["messages_next"] == "c-x j"


def test_message_keybindings_reject_unknown_actions_and_invalid_keys(tmp_path):
    with pytest.raises(ValueError, match="Unknown configurable"):
        update_terminal_preferences(
            workspace=tmp_path, keybindings={"app_exit": "c-q"},
        )
    with pytest.raises(ValueError, match="Invalid key sequence"):
        update_terminal_preferences(
            workspace=tmp_path, keybindings={"messages_next": "banana"},
        )
