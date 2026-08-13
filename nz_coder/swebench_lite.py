"""Compatibility wrapper for `nz_coder.evaluation.swebench_lite`."""
from __future__ import annotations

import runpy as _runpy
import sys as _sys

if __name__ == "__main__":
    _runpy.run_module('nz_coder.evaluation.swebench_lite', run_name="__main__")
else:
    from nz_coder.evaluation import swebench_lite as _impl
    _sys.modules[__name__] = _impl
