"""Compatibility wrapper for `nz_coder.runtime.task_policy`."""
from __future__ import annotations

import sys as _sys
from nz_coder.runtime import task_policy as _impl

_sys.modules[__name__] = _impl
