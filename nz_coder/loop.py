"""Compatibility wrapper for `nz_coder.runtime.execution.loop`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime.execution import loop as _impl

_sys.modules[__name__] = _impl
