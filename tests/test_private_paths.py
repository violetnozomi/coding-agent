"""Cross-platform private-path hardening contracts."""
from __future__ import annotations

import os
from pathlib import Path
import stat


class _FakeWindowsACL:
    def __init__(self, *, available: bool = True, applies: bool = True) -> None:
        self.available = available
        self.applies = applies
        self.private: set[Path] = set()

    def is_available(self) -> bool:
        return self.available

    def apply(self, path: Path, *, is_directory: bool) -> None:
        if not self.applies:
            raise OSError("Access is denied")
        assert path.is_dir() is is_directory
        self.private.add(path.resolve())

    def inspect(self, path: Path) -> bool:
        return path.resolve() in self.private


def test_posix_private_path_applies_owner_only_modes(tmp_path: Path):
    from nz_coder.private_paths import harden_private_path

    directory = tmp_path / "state"
    directory.mkdir(mode=0o777)
    file_path = directory / "token"
    file_path.write_text("secret", encoding="utf-8")
    directory.chmod(0o777)
    file_path.chmod(0o666)

    directory_result = harden_private_path(directory, os_name="posix")
    file_result = harden_private_path(file_path, os_name="posix")

    assert directory_result.hardened is True
    assert directory_result.tier == "A"
    assert file_result.hardened is True
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o600


def test_windows_private_path_reports_verified_acl_not_chmod(tmp_path: Path):
    from nz_coder.private_paths import harden_private_path

    token = tmp_path / "daemon.token"
    token.write_text("secret", encoding="utf-8")
    api = _FakeWindowsACL()

    result = harden_private_path(token, os_name="nt", windows_api=api)

    assert result.hardened is True
    assert result.tier == "A"
    assert "current user" in result.detail


def test_windows_private_path_failure_remains_honest_tier_b(tmp_path: Path):
    from nz_coder.private_paths import harden_private_path

    token = tmp_path / "daemon.token"
    token.write_text("secret", encoding="utf-8")

    result = harden_private_path(
        token,
        os_name="nt",
        windows_api=_FakeWindowsACL(applies=False),
    )

    assert result.hardened is False
    assert result.tier == "B"
    assert "Access is denied" in result.detail
    assert "secret" not in result.detail


def test_windows_private_path_inspection_detects_unhardened_path(tmp_path: Path):
    from nz_coder.private_paths import (
        harden_private_path,
        inspect_private_path,
    )

    token = tmp_path / "daemon.token"
    token.write_text("secret", encoding="utf-8")
    api = _FakeWindowsACL()

    before = inspect_private_path(token, os_name="nt", windows_api=api)
    harden_private_path(token, os_name="nt", windows_api=api)
    after = inspect_private_path(token, os_name="nt", windows_api=api)

    assert before.hardened is False and before.tier == "B"
    assert after.hardened is True and after.tier == "A"


def test_windows_acl_availability_uses_api_probe():
    from nz_coder.private_paths import windows_private_acl_available

    assert windows_private_acl_available(_FakeWindowsACL(available=True)) is True
    assert windows_private_acl_available(_FakeWindowsACL(available=False)) is False


def test_missing_private_path_is_not_reported_as_secure(tmp_path: Path):
    from nz_coder.private_paths import inspect_private_path

    result = inspect_private_path(
        tmp_path / "missing",
        os_name=os.name,
        windows_api=_FakeWindowsACL(),
    )

    assert result.hardened is False
    assert result.tier == "B"


def test_windows_dacl_parser_requires_protected_full_control_for_user_and_system():
    from nz_coder.private_paths import _private_dacl_sddl

    current = "S-1-5-21-1000"
    assert _private_dacl_sddl(
        f"D:P(A;;FA;;;SY)(A;;FA;;;{current})",
        current,
    ) is True
    assert _private_dacl_sddl(
        f"D:P(A;;FR;;;SY)(A;;FA;;;{current})",
        current,
    ) is False
    assert _private_dacl_sddl(
        f"D:P(A;;FA;;;SY)(A;;FA;;;{current})(A;;FR;;;BU)",
        current,
    ) is False
    assert _private_dacl_sddl(
        f"D:(A;;FA;;;SY)(A;;FA;;;{current})",
        current,
    ) is False


def test_windows_private_path_failure_reports_redacted_observed_dacl(tmp_path: Path):
    from nz_coder.private_paths import inspect_private_path

    class ObservedACL(_FakeWindowsACL):
        observed_sddl = "D:PAI(A;;FA;;;SY)(A;;0x1200A9;;;S-1-5-21-123-456-789-1001)"

        def inspect(self, path: Path) -> bool:
            return False

    token = tmp_path / "daemon.token"
    token.write_text("secret", encoding="utf-8")

    result = inspect_private_path(token, os_name="nt", windows_api=ObservedACL())

    assert "D:PAI" in result.detail
    assert "0x1200A9" in result.detail
    assert "S-1-5-21-123-456-789-1001" not in result.detail
    assert "<SID>" in result.detail
