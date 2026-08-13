"""Compatibility wrapper for `nz_coder.intelligence.project_profile`."""
from __future__ import annotations

import sys as _sys
from nz_coder.intelligence import project_profile as _impl

_sys.modules[__name__] = _impl
