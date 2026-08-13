"""Compatibility wrapper for the shared ripgrep process service."""
from __future__ import annotations

import sys as _sys
from nz_coder import ripgrep as _impl

_sys.modules[__name__] = _impl
