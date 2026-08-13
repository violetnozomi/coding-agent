"""Compatibility wrapper for process-level asynchronous helpers."""
from __future__ import annotations

import sys as _sys
from nz_coder import async_utils as _impl

_sys.modules[__name__] = _impl
