"""Contracts for InfCode-style direct shell requests."""
from __future__ import annotations

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.workdir import scoped_workdir


def test_direct_shell_executes_through_permissioned_tool_pipeline(tmp_path):
    from nz_coder.interface.direct_shell import execute_direct_shell

    with scoped_workdir(tmp_path):
        result = execute_direct_shell(
            "printf direct-shell", permissions=PermissionManager("auto"),
        )
    assert result.executed is True
    assert "direct-shell" in result.output
    assert result.name == "bash"


def test_direct_shell_permission_denial_never_dispatches(tmp_path):
    from nz_coder.interface.direct_shell import execute_direct_shell

    permissions = PermissionManager("plan")
    with scoped_workdir(tmp_path):
        result = execute_direct_shell("touch forbidden", permissions=permissions)
    assert result.executed is False
    assert result.permission_denied is True
    assert not (tmp_path / "forbidden").exists()


def test_direct_shell_rejects_empty_command():
    from nz_coder.interface.direct_shell import execute_direct_shell

    with pytest.raises(ValueError, match="non-empty"):
        execute_direct_shell("   ", permissions=PermissionManager("auto"))


def test_direct_shell_result_is_not_injected_into_agent_transcript(tmp_path):
    from nz_coder.interface.direct_shell import execute_direct_shell

    transcript = [{"role": "user", "content": "existing turn"}]
    with scoped_workdir(tmp_path):
        execute_direct_shell(
            "printf isolated", permissions=PermissionManager("auto"),
        )
    assert transcript == [{"role": "user", "content": "existing turn"}]
