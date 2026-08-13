"""Compatibility wrapper for the core recovery service."""
from __future__ import annotations

import sys as _sys
from nz_coder import recovery as _impl

_sys.modules[__name__] = _impl
