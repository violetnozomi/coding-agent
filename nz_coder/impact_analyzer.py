"""Compatibility wrapper for `nz_coder.intelligence.impact_analyzer`."""
from __future__ import annotations

import sys as _sys
from nz_coder.intelligence import impact_analyzer as _impl

_sys.modules[__name__] = _impl
