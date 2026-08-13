"""Compatibility wrapper for `nz_coder.tool_platform.policies.command_policy`."""
from __future__ import annotations

import sys as _sys
from nz_coder.tool_platform.policies import command_policy as _impl

_sys.modules[__name__] = _impl
