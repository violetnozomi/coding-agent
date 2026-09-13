"""Dependency-light language identifiers shared by config and LSP discovery."""
from __future__ import annotations


LSP_LANGUAGES = (
    "python",
    "typescript",
    "go",
    "rust",
    "cpp",
    "java",
    "kotlin",
    "ruby",
    "php",
    "lua",
    "bash",
    "yaml",
    "dart",
)


def lsp_command_config_key(language: str) -> str:
    """Return the explicit command override key for one supported language."""
    normalized = str(language).strip().lower()
    if normalized not in LSP_LANGUAGES:
        raise ValueError(f"unsupported LSP language: {normalized}")
    return f"NZ_LSP_{normalized.upper()}_COMMAND"


__all__ = ["LSP_LANGUAGES", "lsp_command_config_key"]
