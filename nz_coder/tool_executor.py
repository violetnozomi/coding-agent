"""Compatibility wrapper for `nz_coder.runtime.tool_executor`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime import tool_executor as _impl

_sys.modules[__name__] = _impl
