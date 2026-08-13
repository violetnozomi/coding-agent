"""Compatibility wrapper for `nz_coder.intelligence.verification_planner`."""
from __future__ import annotations

import sys as _sys
from nz_coder.intelligence import verification_planner as _impl

_sys.modules[__name__] = _impl
