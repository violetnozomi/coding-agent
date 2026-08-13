"""Compatibility wrapper for `nz_coder.interface.cli`."""
from __future__ import annotations

import sys as _sys
from nz_coder.interface import cli as _impl

_sys.modules[__name__] = _impl
