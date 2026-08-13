"""Architecture boundary tests for removed parallel product surfaces."""
from __future__ import annotations

from pathlib import Path


def test_legacy_dodo_product_sources_are_absent():
    """Dodo/PySide must not silently grow back beside the core Session API."""
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        root / "dodo_server_min.py",
        root / "nz_coder" / "dodo",
        root / "nz_coder" / "pyside_client",
        root / "requirements-dodo.txt",
        root / "requirements-client.txt",
    )
    for path in forbidden:
        if path.is_dir():
            assert not list(path.glob("*.py")), f"legacy product source returned: {path}"
        else:
            assert not path.exists(), f"legacy product source returned: {path}"
