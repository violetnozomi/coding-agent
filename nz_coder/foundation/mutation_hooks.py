"""Context-local injection at controlled file mutation boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.foundation.user_paths import user_storage_layout


_RECORDER: ContextVar[object] = ContextVar("workspace_mutation_recorder", default=None)


@contextmanager
def scoped_mutation_recorder(recorder):
    token = _RECORDER.set(recorder)
    try:
        yield
    finally:
        _RECORDER.reset(token)


@contextmanager
def recorded_mutation(access, path, data, transaction, expected, mode):
    """Dispatch an injected recorder, or enforce the existing recovery gate."""
    recorder = _RECORDER.get()
    if recorder is not None:
        with recorder(access, path, data, transaction, expected, mode) as identity:
            yield identity
        return
    from nz_coder.state.tool_ledger import ToolLedger

    database = user_storage_layout(access.root).workspace_state / "tool-recovery" / "ledger.sqlite3"
    if database.exists():
        ledger = ToolLedger(access.root)
        with exclusive_file_lock(ledger.root / "workspace-write.lock"):
            ledger.assert_writable()
            yield expected
    else:
        yield expected
