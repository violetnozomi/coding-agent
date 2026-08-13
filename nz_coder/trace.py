"""Compatibility wrapper for `nz_coder.state.trace`."""
from __future__ import annotations

import sys as _sys
from nz_coder.state import trace as _impl

_sys.modules[__name__] = _impl
