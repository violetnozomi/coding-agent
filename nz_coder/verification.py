"""Compatibility wrapper for `nz_coder.intelligence.verification`."""
from __future__ import annotations

import sys as _sys
from nz_coder.intelligence import verification as _impl

_sys.modules[__name__] = _impl
