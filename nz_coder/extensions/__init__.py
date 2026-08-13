"""Unified, read-only extension metadata contract."""
from nz_coder.extensions.registry import (
    ExtensionDescriptor,
    ExtensionRegistry,
    extension_snapshot,
)

__all__ = ["ExtensionDescriptor", "ExtensionRegistry", "extension_snapshot"]
