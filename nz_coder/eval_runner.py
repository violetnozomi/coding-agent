"""Compatibility wrapper for `nz_coder.evaluation.eval_runner`."""
from __future__ import annotations

import runpy as _runpy
import sys as _sys

if __name__ == "__main__":
    _runpy.run_module('nz_coder.evaluation.eval_runner', run_name="__main__")
else:
    from nz_coder.evaluation import eval_runner as _impl
    _sys.modules[__name__] = _impl
