"""Compatibility wrapper for `nz_coder.tool_platform.permissioning`."""
from __future__ import annotations

import sys as _sys
from nz_coder.tool_platform import permissioning as _impl

_sys.modules[__name__] = _impl
