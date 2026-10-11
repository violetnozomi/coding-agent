"""Backwards compatibility types, which will be deprecated a future release. Do not use these in new code."""

from __future__ import annotations as _annotations

import typing as _t

from ansible.module_utils._internal import _datatag
from ansible.module_utils.common.text import converters as _converters
from ansible.parsing import vault as _vault


class _AnsibleMapping(dict):
    """Backwards compatibility type."""

    def __new__(cls, value=None, /, **kwargs):
        # Match the base `dict` construction patterns: no argument yields an empty mapping, an initial
        # mapping/iterable may be given, and additional keyword pairs may be supplied alongside it.
        if value is None:
            value = {}

        src = value
        result = dict(value, **kwargs)

        return _datatag.AnsibleTagHelper.tag_copy(src, result)


class _AnsibleUnicode(str):
    """Backwards compatibility type."""

    def __new__(cls, *args, **kwargs):
        # Delegate to `str` so that the value can be supplied positionally or via the `object` keyword, and so that
        # `encoding`/`errors` are only forwarded when the caller explicitly supplied them (no hardcoded defaults).
        if args:
            src = args[0]
        elif 'object' in kwargs:
            src = kwargs['object']
        else:
            src = ''

        value = str(*args, **kwargs)

        return _datatag.AnsibleTagHelper.tag_copy(src, value)


class _AnsibleSequence(list):
    """Backwards compatibility type."""

    def __new__(cls, value=()):
        # No argument yields an empty list; an iterable yields a list equal to that iterable.
        return _datatag.AnsibleTagHelper.tag_copy(value, list(value))


class _AnsibleVaultEncryptedUnicode:
    """Backwards compatibility type."""

    def __new__(cls, ciphertext: str | bytes):
        encrypted_string = _vault.EncryptedString(ciphertext=_converters.to_text(_datatag.AnsibleTagHelper.untag(ciphertext)))

        return _datatag.AnsibleTagHelper.tag_copy(ciphertext, encrypted_string)


def __getattr__(name: str) -> _t.Any:
    """Inject import-time deprecation warnings."""
    if (value := globals().get(f'_{name}', None)) and name.startswith('Ansible'):
        # deprecated: description='enable deprecation of everything in this module', core_version='2.23'
        # from ansible.utils.display import Display
        #
        # Display().deprecated(
        #     msg=f"Importing {name!r} is deprecated.",
        #     help_text="Instances of this type cannot be created and will not be encountered.",
        #     version="2.27",
        # )

        return value

    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
