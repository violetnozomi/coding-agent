"""Architecture guards for product entry-point runtime convergence."""
from __future__ import annotations

import inspect


def test_http_default_path_does_not_default_to_legacy_agent_factory():
    from nz_coder.http_service import manager

    source = inspect.getsource(manager.SessionManager.__init__)
    assert "agent_factory or build_http_agent" not in source


def test_http_managed_session_runs_native_client_path():
    from nz_coder.http_service import manager

    source = inspect.getsource(manager.ManagedSession._run_agent)
    assert "self.client.run" in source
