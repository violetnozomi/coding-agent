"""LSP状态投影只呈现当前workspace的真实entry。"""

from nz_coder.lsp import manager
from tests.test_lsp import _registered_client
from types import SimpleNamespace


def test_client_status_summary_is_workspace_scoped_and_secret_free(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first = _registered_client(
        tmp_path,
        SimpleNamespace(_closed=False, process=SimpleNamespace(poll=lambda: None)),
    )
    second = _registered_client(
        other,
        SimpleNamespace(_closed=False, process=SimpleNamespace(poll=lambda: None)),
    )
    try:
        rows = manager.client_status_summary(tmp_path)
        assert len(rows) == 1
        assert rows[0]["entry_id"] == first.entry_id
        assert rows[0]["status"] == "ready"
        assert second.entry_id not in repr(rows)
        assert "environment" not in rows[0]
    finally:
        manager._ENTRIES.pop(first.key, None)
        manager._ENTRIES.pop(second.key, None)
