"""Compatibility wrapper for `nz_coder.state.sessions`."""
from __future__ import annotations

import sys as _sys
from nz_coder.state import sessions as _impl
from nz_coder.runtime.session_cleanup import install_session_cleanup as _install_cleanup

_install_cleanup()

_sys.modules[__name__] = _impl
