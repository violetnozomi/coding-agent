"""Opaque string-compatible values with redacted diagnostic representations."""
from __future__ import annotations


class SecretStr(str):
    """A runtime string whose ``repr`` never exposes its underlying value.

    The object remains string-compatible at provider and subprocess boundaries.
    Dataclass diagnostics, including ``dataclasses.asdict(...)``, receive a
    redacted copy. Run-owned credential objects are intentionally not a secret
    cloning mechanism; consumers must use the original value at the boundary.
    """

    def __new__(cls, value: object = "") -> "SecretStr":
        return super().__new__(cls, "" if value is None else str(value))

    def __repr__(self) -> str:
        return "'<redacted>'" if self else "''"

    def __deepcopy__(self, memo: dict[int, object]) -> "SecretStr":
        """Return a redacted diagnostic copy instead of duplicating a secret."""
        del memo
        return type(self)("<redacted>" if self else "")


def secret_str(value: object = "") -> SecretStr:
    """Return ``value`` as a diagnostic-safe string without double wrapping."""
    return value if isinstance(value, SecretStr) else SecretStr(value)


__all__ = ["SecretStr", "secret_str"]
