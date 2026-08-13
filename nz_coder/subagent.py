"""Compatibility wrapper for `nz_coder.runtime.subagent`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime import subagent as _impl

_sys.modules[__name__] = _impl
