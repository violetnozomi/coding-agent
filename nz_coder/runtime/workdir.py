"""Compatibility wrapper for the state-owned workspace context."""
from __future__ import annotations

import sys as _sys
from nz_coder.state import workdir as _impl

_sys.modules[__name__] = _impl
