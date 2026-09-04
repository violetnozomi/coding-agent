"""Static contracts that keep the final security-boundary closure intact."""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _contains_raw_exception_projection(node: ast.AST, name: str) -> bool:
    def exception_root(value: ast.AST) -> bool:
        current = value
        while isinstance(current, ast.Attribute):
            if current.attr == "code":
                return False
            current = current.value
        return isinstance(current, ast.Name) and current.id == name

    for child in ast.walk(node):
        if (
            isinstance(child, ast.FormattedValue)
            and exception_root(child.value)
        ):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in {"str", "repr"}
            and child.args
            and exception_root(child.args[0])
        ):
            return True
    return False


def test_public_returns_never_format_caught_exceptions_directly():
    """A returned model/tool value must use the explicit PublicError boundary."""
    roots = tuple(
        ROOT / "nz_coder" / name
        for name in ("runtime", "tools", "capabilities", "providers", "mcp", "lsp")
    )
    violations: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for handler in ast.walk(tree):
                if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                    continue
                for child in ast.walk(handler):
                    if (
                        isinstance(child, ast.Return)
                        and child.value is not None
                        and _contains_raw_exception_projection(
                            child.value, handler.name,
                        )
                    ):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{child.lineno}: "
                            f"returned caught exception {handler.name!r}; "
                            "project it through PublicError/PublicInputError"
                        )
    assert not violations, "\n".join(violations)


def test_capability_lease_identity_is_complete_and_immutable():
    from nz_coder.foundation.capability_lease import CapabilityLease

    assert CapabilityLease.__dataclass_params__.frozen is True
    assert {field.name for field in fields(CapabilityLease)} == {
        "lease_id",
        "kind",
        "resource_id",
        "workspace_identity",
        "control_fingerprint",
        "run_id",
        "interaction_id",
        "created_at",
        "owner_session",
    }


def test_provider_connection_carries_both_authority_sources():
    from nz_coder.providers.configuration import ProviderConnection

    names = {field.name for field in fields(ProviderConnection)}
    assert {"credential_source", "endpoint_source", "credential_scope_id"} <= names


def test_local_protocol_limits_are_finite_and_nontrivial():
    from nz_coder.lsp import client as lsp
    from nz_coder.mcp import client as mcp

    assert 1024 <= lsp._MAX_HEADER_BYTES <= 1024 * 1024
    assert lsp._MAX_HEADER_BYTES < lsp._MAX_FRAME_BYTES <= 32 * 1024 * 1024
    assert 1 <= lsp._MAX_DIAGNOSTICS <= 10_000
    assert 1024 <= lsp._MAX_DIAGNOSTIC_BYTES <= lsp._MAX_FRAME_BYTES
    assert 1024 <= mcp._MAX_FRAME_BYTES <= 32 * 1024 * 1024


def test_private_lock_keeps_platform_anti_alias_primitives():
    source = (ROOT / "nz_coder/foundation/file_lock.py").read_text(
        encoding="utf-8"
    )
    assert "O_NOFOLLOW" in source
    assert "dir_fd=" in source
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in source
    assert "S_ISREG" in source


def test_public_error_protocol_has_explicit_input_error_type():
    from nz_coder.protocol.public_error import PublicInputError, to_public_error

    public = to_public_error(PublicInputError("A predefined validation error."))
    assert public.code == "invalid_input"
    assert public.message == "A predefined validation error."
