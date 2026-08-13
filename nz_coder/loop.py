"""Compatibility wrapper for `nz_coder.runtime.loop`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime import loop as _impl

_sys.modules[__name__] = _impl
