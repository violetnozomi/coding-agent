"""Compatibility wrapper for `nz_coder.state.skills`."""
from __future__ import annotations

import sys as _sys
from nz_coder.state import skills as _impl

_sys.modules[__name__] = _impl
