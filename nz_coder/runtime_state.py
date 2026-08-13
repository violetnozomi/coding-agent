"""Compatibility wrapper for `nz_coder.runtime.runtime_state`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime import runtime_state as _impl

_sys.modules[__name__] = _impl
